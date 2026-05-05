"""Auto-register all tools on import."""
from elian_agent_cc.tools.file_tools import FileReadTool, FileWriteTool, FileEditTool, GlobTool, GrepTool
from elian_agent_cc.tools.bash_tool import BashTool
from elian_agent_cc.tools.web_tools import WebFetchTool, WebSearchTool
from elian_agent_cc.tools.ask_user_question import AskUserQuestionTool
from elian_agent_cc.tools.plan_mode import EnterPlanModeTool, ExitPlanModeTool
from elian_agent_cc.tools.agent_tool import AgentTool
from elian_agent_cc.tools.agent_comms import SendMessageTool, TaskOutputTool, TaskStopTool
from elian_agent_cc.tools.task_management import TaskCreateTool, TaskGetTool, TaskListTool, TaskUpdateTool, TodoWriteTool
from elian_agent_cc.tools.worktree_tools import EnterWorktreeTool, ExitWorktreeTool
from elian_agent_cc.tools.more_tools import (
    NotebookEditTool, CronCreateTool, CronDeleteTool, CronListTool,
    ConfigTool, ToolSearchTool, SyntheticOutputTool, BriefTool, LSPTool,
    TeamCreateTool, TeamDeleteTool,
)
