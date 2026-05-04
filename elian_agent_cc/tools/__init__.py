"""Auto-register all tools on import."""
from tools.file_tools import FileReadTool, FileWriteTool, FileEditTool, GlobTool, GrepTool
from tools.bash_tool import BashTool
from tools.ask_user_question import AskUserQuestionTool
from tools.plan_mode import EnterPlanModeTool, ExitPlanModeTool
from tools.agent_comms import SendMessageTool, TaskOutputTool, TaskStopTool
