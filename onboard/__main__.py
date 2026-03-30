from .extra import Config
from rich.console import Console
from rich.panel import Panel
from rich.markdown import Markdown

c = Config('settings.yaml')
c.set('server/port', 8080)
c.set('server/hostname', 'localhost')