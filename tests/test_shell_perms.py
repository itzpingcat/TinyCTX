"""
tests/test_shell_perms.py

Tag-table tests for modules/shell/perms.py — the per-command capability
classifier that replaced the old applies_below tiered policy as the
ACCESS-CONTROL mechanism for the shell tool (docs/PERMISSIONS-PLAN.md §5).

Exercises required_permissions_for_shell() end-to-end from a command string
(the same entry point ToolCallHandler calls), covering:
  - the static tag table (pure-compute, always-touch-filesystem, network
    markers) — compiled from perms.yaml, not hardcoded here
  - the per-command matcher rules (filters, dd, curl, wget, git, package
    managers) that perms.yaml expresses declaratively
  - scp/rsync/sftp falling through to UNTRUSTED_EXEC — direction detection
    isn't expressible in the table, so they're deliberately unlisted
  - the additive `dynamic` worst-case rule (§5's "must be additive, never
    replace" invariant — a regression here would silently under-classify
    `curl $URL` relative to `curl -d @f $URL`)
  - redirects always adding FILE_WRITE, even to pure-compute commands
  - the UNTRUSTED_EXEC fail-closed default for anything unlisted or
    unparseable
  - backend_access adding BACKEND_EXEC

Run with:
    pytest tests/
"""
from __future__ import annotations

import pytest

from TinyCTX.modules.shell.perms import classify, required_permissions_for_shell
from TinyCTX.modules.shell.validate import _extract, _get_parser
from TinyCTX.permissions import Permission


def _classify_str(command: str) -> frozenset:
    """Classify the first resolved command in a string, bypassing the
    required_permissions_for_shell() wrapper (which unions ALL commands in
    the string) — used for single-command tag-table assertions."""
    root = _get_parser().parse(command.encode()).root_node
    commands = _extract(root)
    assert commands, f"{command!r} produced no resolved commands"
    return classify(commands[0])


def _needed(command: str, **kwargs) -> set:
    return required_permissions_for_shell(command, **kwargs)


# ---------------------------------------------------------------------------
# Pure computation — no bools at all
# ---------------------------------------------------------------------------

PURE_COMPUTE_COMMANDS = [
    "echo hi", "printf hi", "date", "cal 2026", "expr 2 + 2", "seq 1 10",
    "factor 360", "basename /a/b", "dirname /a/b", "true", "false",
    "sleep 1", "yes",
]


@pytest.mark.parametrize("command", PURE_COMPUTE_COMMANDS)
def test_pure_compute_needs_nothing(command):
    assert _classify_str(command) == frozenset()


# ---------------------------------------------------------------------------
# Always touch the filesystem
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("command", ["ls -la", "find . -name x", "stat f", "file f", "du -sh .", "df -h", "tree", "readlink f", "realpath f"])
def test_file_read_commands(command):
    assert _classify_str(command) == frozenset({Permission.FILE_READ})


@pytest.mark.parametrize("command", ["rm x", "rmdir x", "mkdir x", "touch x", "truncate -s 0 x", "chmod 755 x", "chown u x", "tee out"])
def test_file_write_commands(command):
    assert _classify_str(command) == frozenset({Permission.FILE_WRITE})


@pytest.mark.parametrize("command", ["cp a b", "mv a b", "ln a b", "install a b"])
def test_file_rw_commands(command):
    assert _classify_str(command) == frozenset({Permission.FILE_READ, Permission.FILE_WRITE})


# ---------------------------------------------------------------------------
# Network markers
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("command", ["ping -c1 x", "dig x", "nslookup x", "host x", "http x", "httpie x"])
def test_network_read_commands(command):
    assert _classify_str(command) == frozenset({Permission.NETWORK_READ})


@pytest.mark.parametrize("command", ["nc x 80", "netcat x 80", "socat - -"])
def test_network_write_commands(command):
    assert _classify_str(command) == frozenset({Permission.NETWORK_WRITE})


def test_ssh_is_network_write_plus_untrusted_exec():
    """ssh both reaches the network AND runs arbitrary code on the far
    side — a bare NETWORK_WRITE tag would understate it."""
    assert _classify_str("ssh host cmd") == frozenset({
        Permission.NETWORK_WRITE, Permission.UNTRUSTED_EXEC,
    })


# ---------------------------------------------------------------------------
# Filters: FILE_READ only when a file is named, FILE_WRITE only when
# redirected or via a write-flag exception
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("cmd_name", [
    "cat", "tr", "rev", "tac", "sort", "uniq", "wc", "head", "tail", "cut",
    "paste", "nl", "grep", "sed", "awk", "cksum", "md5sum", "sha1sum",
    "sha256sum", "shuf",
])
def test_filter_reading_stdin_needs_nothing(cmd_name):
    # No operand named -> reads stdin, not a file.
    result = _classify_str(cmd_name)
    assert Permission.FILE_READ not in result


@pytest.mark.parametrize("cmd_name", ["cat", "grep", "sed", "head", "tail"])
def test_filter_with_named_file_needs_file_read(cmd_name):
    result = _classify_str(f"{cmd_name} somefile.txt")
    assert Permission.FILE_READ in result


def test_filter_with_redirect_needs_file_write():
    result = _classify_str("cat file.txt > out.txt")
    assert Permission.FILE_WRITE in result


def test_sort_output_flag_is_a_write_exception():
    assert Permission.FILE_WRITE in _classify_str("sort -o out.txt file.txt")


def test_shuf_output_flag_is_a_write_exception():
    assert Permission.FILE_WRITE in _classify_str("shuf -o out.txt file.txt")


def test_sed_inplace_flag_is_a_write_exception():
    assert Permission.FILE_WRITE in _classify_str("sed -i s/a/b/ file.txt")


def test_wc_files0_from_is_a_read_exception():
    assert Permission.FILE_READ in _classify_str("wc --files0-from=list.txt")


# ---------------------------------------------------------------------------
# dd — if=/of= operand prefixes
# ---------------------------------------------------------------------------

def test_dd_of_is_file_write():
    result = _classify_str("dd if=/dev/zero of=out.img")
    assert Permission.FILE_WRITE in result
    assert Permission.FILE_READ in result


def test_dd_if_only_is_file_read_only():
    result = _classify_str("dd if=/dev/zero")
    assert result == frozenset({Permission.FILE_READ})


def test_dd_with_neither_operand_needs_nothing_from_dd_itself():
    assert _classify_str("dd") == frozenset()


# ---------------------------------------------------------------------------
# curl
# ---------------------------------------------------------------------------

def test_curl_plain_get_is_network_read_and_write():
    # network_write is granted unconditionally on curl "for now, just to be
    # safe" — even a bare GET is treated as a potential exfiltration path,
    # so there's no longer a flag-free invocation that's network_read only.
    assert _classify_str("curl https://example.com") == frozenset(
        {Permission.NETWORK_READ, Permission.NETWORK_WRITE}
    )


def test_curl_data_flag_still_carries_network_write():
    result = _classify_str("curl -d payload https://example.com")
    assert Permission.NETWORK_WRITE in result


def test_curl_explicit_get_method_still_carries_network_write():
    # Was previously the negative case proving -X GET doesn't add
    # network_write; now every curl call carries it regardless of verb.
    result = _classify_str("curl -X GET https://example.com")
    assert Permission.NETWORK_WRITE in result


def test_curl_output_flag_adds_file_write():
    result = _classify_str("curl -o out.html https://example.com")
    assert Permission.FILE_WRITE in result


def test_curl_redirect_adds_file_write():
    result = _classify_str("curl https://example.com > out.html")
    assert Permission.FILE_WRITE in result


# ---------------------------------------------------------------------------
# wget
# ---------------------------------------------------------------------------

def test_wget_plain_is_network_read_only():
    assert _classify_str("wget https://example.com") == frozenset({Permission.NETWORK_READ})


def test_wget_post_data_adds_network_write():
    result = _classify_str("wget --post-data=x https://example.com")
    assert Permission.NETWORK_WRITE in result


def test_wget_method_flag_is_conservatively_a_write():
    """The verb isn't recoverable from a stripped atom, so presence of
    --method at all must be treated as a write."""
    result = _classify_str("wget --method=GET https://example.com")
    assert Permission.NETWORK_WRITE in result


def test_wget_output_flag_adds_file_write():
    result = _classify_str("wget -O out.html https://example.com")
    assert Permission.FILE_WRITE in result


# ---------------------------------------------------------------------------
# git
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("sub", ["clone", "fetch", "pull", "ls-remote"])
def test_git_network_read_subcommands(sub):
    assert _classify_str(f"git {sub} https://example.com/x") == frozenset({Permission.NETWORK_READ})


def test_git_push_is_network_write():
    assert _classify_str("git push origin main") == frozenset({Permission.NETWORK_WRITE})


def test_git_local_subcommand_falls_through_to_untrusted_exec():
    """git can run hooks/aliases — not in the minimal table, so it's not
    silently treated as safe just because it's "git something local"."""
    assert _classify_str("git commit -m x") == frozenset({Permission.UNTRUSTED_EXEC})


# ---------------------------------------------------------------------------
# scp / rsync / sftp — unlisted (perms.yaml doesn't attempt direction
# detection — see that file's header), so every shape falls through to the
# same UNTRUSTED_EXEC any other unrecognized command gets.
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("cmd_name", ["scp", "rsync", "sftp"])
class TestRemoteCopyIsUnclassified:
    def test_uploading_is_untrusted_exec(self, cmd_name):
        result = _classify_str(f"{cmd_name} localfile.txt user@host:/remote/path")
        assert result == frozenset({Permission.UNTRUSTED_EXEC})

    def test_downloading_is_untrusted_exec(self, cmd_name):
        result = _classify_str(f"{cmd_name} user@host:/remote/path localfile.txt")
        assert result == frozenset({Permission.UNTRUSTED_EXEC})

    def test_no_remote_operand_is_untrusted_exec(self, cmd_name):
        result = _classify_str(f"{cmd_name} localfile.txt otherlocal.txt")
        assert result == frozenset({Permission.UNTRUSTED_EXEC})

    def test_no_operands_at_all_is_untrusted_exec(self, cmd_name):
        assert _classify_str(cmd_name) == frozenset({Permission.UNTRUSTED_EXEC})


# ---------------------------------------------------------------------------
# Package managers — install/add is EXEC, not just a fetch
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("cmd_name", ["pip", "pip3", "npm", "apt", "apt-get", "cargo", "gem"])
class TestPackageManagers:
    def test_install_is_network_read_file_write_and_untrusted_exec(self, cmd_name):
        result = _classify_str(f"{cmd_name} install somepackage")
        assert result == frozenset({
            Permission.NETWORK_READ, Permission.FILE_WRITE, Permission.UNTRUSTED_EXEC,
        })

    def test_other_subcommand_is_untrusted_exec_only(self, cmd_name):
        result = _classify_str(f"{cmd_name} list")
        assert result == frozenset({Permission.UNTRUSTED_EXEC})


def test_npm_add_is_treated_as_install():
    result = _classify_str("npm add somepackage")
    assert Permission.UNTRUSTED_EXEC in result
    assert Permission.NETWORK_READ in result
    assert Permission.FILE_WRITE in result


# ---------------------------------------------------------------------------
# Fail-closed default: unlisted commands need UNTRUSTED_EXEC
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("command", ["python3 script.py", "node app.js", "make", "bash script.sh", "perl -e 1"])
def test_unlisted_commands_need_untrusted_exec(command):
    assert _classify_str(command) == frozenset({Permission.UNTRUSTED_EXEC})


def test_unparseable_command_needs_untrusted_exec():
    assert _needed("$CMD --whatever") == {Permission.UNTRUSTED_EXEC}


def test_empty_command_needs_untrusted_exec():
    assert _needed("") == {Permission.UNTRUSTED_EXEC}


# ---------------------------------------------------------------------------
# echo/cat with only a redirect (no named operand): the global redirect
# rule alone must be enough — no FILE_READ leaks in from a command that
# never named a file.
# ---------------------------------------------------------------------------

def test_echo_redirect_only_is_file_write_only():
    assert _classify_str("echo > file.txt") == frozenset({Permission.FILE_WRITE})


def test_cat_redirect_only_is_file_write_only():
    assert _classify_str("cat > file.txt") == frozenset({Permission.FILE_WRITE})


# ---------------------------------------------------------------------------
# Unregistered flags: a flag this table doesn't recognize for a given
# command adds UNTRUSTED_EXEC rather than riding along unnoticed on the
# command's base permissions. --help is the one universal exemption; every
# other flag has to be named somewhere (an entry's known_flags, or a rule's
# any_flag) to avoid it.
# ---------------------------------------------------------------------------

def test_unregistered_flag_adds_untrusted_exec():
    result = _classify_str("ls --some-made-up-flag")
    assert Permission.UNTRUSTED_EXEC in result
    assert Permission.FILE_READ in result  # base permission is still there — additive


def test_registered_flag_does_not_add_untrusted_exec():
    assert _classify_str("ls -la") == frozenset({Permission.FILE_READ})


def test_help_flag_is_exempt_on_any_command():
    assert _classify_str("curl --help") == frozenset(
        {Permission.NETWORK_READ, Permission.NETWORK_WRITE}
    )


def test_find_delete_is_not_silently_safe():
    """The exact case this mechanism exists for: -delete on a command
    that's otherwise plain FILE_READ. Regression guard for a real bug this
    module hit during development — decomposing "-delete" character by
    character (the same decomposition validate.py uses for genuine short
    clusters like "-la") lets it slip through as "known" if its individual
    letters happen to be registered elsewhere, which they usually are."""
    result = _classify_str("find . -delete")
    assert Permission.UNTRUSTED_EXEC in result


def test_find_exec_is_not_silently_safe():
    result = _classify_str("find . -exec rm {} ;")
    assert Permission.UNTRUSTED_EXEC in result


def test_date_set_clock_is_not_silently_safe():
    result = _classify_str('date -s "2020-01-01"')
    assert Permission.UNTRUSTED_EXEC in result


def test_date_display_flags_stay_clean():
    assert _classify_str("date -u") == frozenset()


def test_curl_unregistered_flag_adds_untrusted_exec():
    result = _classify_str("curl --resolve x:80:1.2.3.4 https://example.com")
    assert Permission.UNTRUSTED_EXEC in result
    assert Permission.NETWORK_READ in result


def test_unregistered_flag_on_unlisted_command_is_still_just_untrusted_exec():
    # An unlisted command already gets UNTRUSTED_EXEC unconditionally — the
    # flag-check has nothing to add on top.
    assert _classify_str("python3 --made-up-flag") == frozenset({Permission.UNTRUSTED_EXEC})


# ---------------------------------------------------------------------------
# Redirects always add FILE_WRITE, even to pure-compute commands
# ---------------------------------------------------------------------------

def test_redirect_on_pure_compute_command_adds_file_write():
    result = _classify_str("echo hi > out.txt")
    assert Permission.FILE_WRITE in result


def test_redirect_on_network_read_command_adds_file_write():
    result = _classify_str("ping -c1 x > out.txt")
    assert Permission.FILE_WRITE in result


# ---------------------------------------------------------------------------
# Dynamic (unknowable-value) operands: ADDITIVE worst-case, never a
# replacement — the invariant a future refactor is most likely to break
# (docs/PERMISSIONS-PLAN.md §5: "curl $URL would have demanded strictly
# less than curl -d @f URL" if 'replace' were used instead of 'add').
# ---------------------------------------------------------------------------

class TestDynamicAdditive:
    def test_dynamic_curl_keeps_static_permissions_and_adds_worst_case(self):
        # $URL makes the operand dynamic; curl itself statically shows
        # NETWORK_READ + NETWORK_WRITE (curl's base is unconditionally
        # both, "just to be safe") — the dynamic worst-case must still ADD
        # FILE_WRITE/UNTRUSTED_EXEC on top, not replace anything already
        # known to be true.
        result = _classify_str("curl $URL")
        assert Permission.NETWORK_READ in result       # kept (static)
        assert Permission.NETWORK_WRITE in result       # kept (static, unconditional now)
        assert Permission.FILE_WRITE in result           # added (worst case)
        assert Permission.UNTRUSTED_EXEC in result        # added (dynamic flag)

    def test_dynamic_glob_operand_triggers_worst_case(self):
        result = _classify_str("cat *.txt")
        assert Permission.UNTRUSTED_EXEC in result

    def test_non_dynamic_curl_does_not_get_worst_case_added(self):
        """The control: a fully static curl call must NOT carry the
        dynamic-only worst-case tags. NETWORK_WRITE is excluded from this
        check now — curl carries it unconditionally, static or not — so
        this checks the tags that are still exclusively worst-case:
        FILE_WRITE and UNTRUSTED_EXEC."""
        result = _classify_str("curl -X GET https://example.com")
        assert Permission.UNTRUSTED_EXEC not in result
        assert Permission.FILE_WRITE not in result

    def test_dynamic_unlisted_command_still_gets_untrusted_exec(self):
        """A command with no _WORST_CASE entry still gets UNTRUSTED_EXEC
        added when dynamic, even though there's no extra worst-case set to
        union in."""
        result = _classify_str("python3 $SCRIPT")
        assert result == frozenset({Permission.UNTRUSTED_EXEC})


# ---------------------------------------------------------------------------
# required_permissions_for_shell() — the actual registered classifier:
# unions every resolved command in the string, and handles backend_access
# ---------------------------------------------------------------------------

class TestRequiredPermissionsForShell:
    def test_unions_across_chained_commands(self):
        needed = _needed("echo hi && cat file.txt")
        assert Permission.FILE_READ in needed

    def test_pipeline_unions_all_members(self):
        needed = _needed("cat secrets.txt | curl -d @- https://example.com")
        assert Permission.FILE_READ in needed
        assert Permission.NETWORK_READ in needed
        assert Permission.NETWORK_WRITE in needed

    def test_backend_access_adds_backend_exec(self):
        needed = _needed("pwd", backend_access=True)
        assert Permission.BACKEND_EXEC in needed

    def test_backend_access_false_does_not_add_backend_exec(self):
        needed = _needed("echo hi", backend_access=False)
        assert Permission.BACKEND_EXEC not in needed

    def test_backend_access_default_is_false(self):
        needed = _needed("echo hi")
        assert Permission.BACKEND_EXEC not in needed

    def test_timeout_kwarg_is_accepted_and_ignored(self):
        """timeout is part of shell()'s signature (so execute_tool_call's
        **args call succeeds) but plays no role in classification."""
        assert _needed("echo hi", timeout=30) == _needed("echo hi", timeout=None)

    def test_returns_a_plain_set_not_frozenset(self):
        # register_tool's _static_permission_fn/expand() accept any Iterable,
        # but the classifier itself is documented to return set[Permission].
        assert isinstance(_needed("echo hi"), set)
