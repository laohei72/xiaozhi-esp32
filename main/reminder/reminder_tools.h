#ifndef REMINDER_TOOLS_H_
#define REMINDER_TOOLS_H_

class ReminderManager;

/**
 * Registers the reminder tools (self.reminders.*) on the device MCP server.
 *
 * Kept out of McpServer::AddCommonTools() so the reminder feature owns its whole surface: the MCP
 * dispatcher stays transport-only, and the tools can only reach the manager through the public
 * ReminderManager API (no Application state, no audio calls).
 *
 * Call once, in the main task, after McpServer::AddCommonTools() and before the tool inventory is
 * logged. |manager| must outlive the server, which is why Application passes its own member.
 */
void RegisterReminderTools(ReminderManager& manager);

#endif  // REMINDER_TOOLS_H_
