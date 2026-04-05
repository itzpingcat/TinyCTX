# Plan to improve TinyCTX's onboarding

## What it aims to solve

- User friendlyness
- Better maintainibility
- No monolithic onboard file

## What onboarding should look like

### Step 0: Beginner or Advanced? (__main__.py)

- Ask the user if they are a Beginner with AI Agents or a Pro.
- Both will be asked the same questions, BUT
  - For beginners, only a few providers are exposed
  - Instructions are explained more in-depth (Eg, what's an API key, keep it safe, etc)
- Call the following setup files in order: Providers, Bridges, Workspace, Gateway
- Check for existing config: let user choose reset or overwrite if found

### Step 1: Providers (providers_setup.py)

- Allow the user to pick a provider or choose their own URL.
- Check if the environment variable is set. If not, tell the user to get an API key. (Or skip if it doesn't require an API key)
- Set the environment variable.
- Send a list models request to the chosen provider.
- If it fails;
  - Reset the api key environment variable.
  - Notify the user that it failed, either due to invalid API key or unresponsive API.
  - Make them go back to pick another provider.
- Else, continue on.
- Take the list models response and display it.
- Let the user pick a model.

- Ask the user if they want embeddings.
- If yes, do the same for embedding providers.

### Step 2: Bridges (bridges_setup.py)

- There is a /onboard/bridges directory with each bridge setup code in a .py file. Eg. discord.py, matrix.py, telegram.py.
- Allow the user to pick bridges. Through a check-box like interface.
- The user can skip bridges and configure them later.
- Now run each bridge setup file (in /onboard/bridges/) that coresponds with each bridge.

### Step 3: Workspace (workspace_setup.py)

- Allow the user to choose the file path where the agent workspace is. Blank for default /.tinyctx/
- If the workspace is completely empty or missing (no files at all), unpack BOOTSTRAP.md from
- For each of the following, check if it is already exists in the workspace path. If not, do the following.
  - Quietly unpack boilerplate AGENTS.md from /onboard/bundled/
  - Quietly unpack boilerplate SOUL.md from /onboard/bundled/
  - Quietly unpack boilerplate MEMORY.md from /onboard/bundled/
  - Quietly unpack cron skill from /onboard/bundled/skills/cron
- Ask the user if they want additional reccomended skills through a checkbox-type interface. (stored in bundled)
  - Clawhub skill
  - Weather skill
  - Skill creator

### Step 4: Gateway (gateway_setup.py)

- Ask the user what port to listen on.
- Validate the port is open. If not, make the user choose another port.
- Auto-generate a gateway api key.
- Launch the gateway.
- Health check it every second till it is healthy.
- If it doesn't respond after 15 sec, tell the user to report the issue to the TinyCTX repository.
- Else, tell the user that they will continue configuration by chatting with their agent.
- Launch the CLI bridge.

## Global conventions

- Users can Ctrl C at any time to cancel onboarding.
- Users can undo their last decision.
- Very robust input. It should not break if the user types something unexpected.
