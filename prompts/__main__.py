"""Print the assembled system prompt exactly as the model receives it:

    .venv\\Scripts\\python.exe -m prompts
"""

from . import system_prompt

print(system_prompt())
