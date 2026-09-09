/** A front-end-only catalogue of the slash commands handled by CommandRouter. */
export type SlashCommandSuggestion = {
  usage: string;
  insertText: string;
  description: string;
};

export const SLASH_COMMAND_SUGGESTIONS: readonly SlashCommandSuggestion[] = [
  {
    usage: "/new",
    insertText: "/new",
    description: "清空当前会话历史，不会创建新的 session。",
  },
  {
    usage: "/stop",
    insertText: "/stop",
    description: "停止当前会话正在执行的请求。",
  },
  {
    usage: "/help",
    insertText: "/help",
    description: "显示后端当前已注册的命令。",
  },
  {
    usage: "/goal <目标描述>",
    insertText: "/goal ",
    description: "创建并开始执行当前会话的持续目标。",
  },
  {
    usage: "/goal status",
    insertText: "/goal status",
    description: "查看当前会话的目标状态。",
  },
  {
    usage: "/goal stop",
    insertText: "/goal stop",
    description: "停止当前会话正在执行的目标。",
  },
  {
    usage: "/compact",
    insertText: "/compact",
    description: "将当前会话较早的完整历史整理为摘要。",
  },
  {
    usage: "/memory",
    insertText: "/memory",
    description: "查看当前 workspace 的长期记忆。",
  },
  {
    usage: "/subagents",
    insertText: "/subagents",
    description: "列出当前会话的后台子 Agent 任务。",
  },
  {
    usage: "/subagents status <task_id>",
    insertText: "/subagents status ",
    description: "查看指定后台子 Agent 任务的状态。",
  },
  {
    usage: "/subagents cancel <task_id>",
    insertText: "/subagents cancel ",
    description: "取消指定后台子 Agent 任务。",
  },
];

/** Return matching suggestions only while the user is composing a slash command. */
export function getSlashCommandSuggestions(
  input: string,
): readonly SlashCommandSuggestion[] {
  const query = input.trimStart().toLocaleLowerCase();
  if (!query.startsWith("/")) {
    return [];
  }
  return SLASH_COMMAND_SUGGESTIONS.filter((suggestion) =>
    suggestion.usage.toLocaleLowerCase().startsWith(query),
  );
}
