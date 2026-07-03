# MCP tool descriptions: overview, examples, and best practices

> Source: https://www.merge.dev/blog/mcp-tool-description

# MCP tool descriptions: overview, examples, and best practices
URL: https://www.merge.dev/blog/mcp-tool-description
Published: 2025-11-05

Jon Gitlin — Senior Content Marketing Manager at Merge

To help agents call the right tools consistently, Model Context Protocol (MCP) servers’ tools need comprehensive descriptions.

We’ll cover what these descriptions can look like across MCP servers, as well as best practices for writing your own. But first, let’s align on how MCP tool descriptions work.

## MCP tool description overview

An MCP tool description is a concise (1-2 sentence) breakdown of how a particular MCP tool functions. It can also include instructions on how the tool handles operations like authentication, pagination, and filtering, but these details should generally be addressed in the tool’s schema.

Most MCP tool descriptions are 1 to 2 sentences and are structured around a verb and a resource

MCP tools are primarily used when an agent receives an input and needs to select the appropriate tool.

For example, if a user asks an agent to create a support ticket for a product bug, the agent can first call list_tools or list_resources to see all of the connected MCP servers’ tools.

The agent can then read all of the tool descriptions exposed and select the one that most closely matches the action it’s looking to take. In this case, it could be create_ticket for Linear’s MCP server.

Note: In rare cases where a tool description includes operational details like pagination limits or authentication requirements, the agent can use that context to shape its tool call arguments. For example, if the description specifies that the tool can only fetch 10 items per call, the agent may include max_results:10 in its arguments.

Related: A guide to using MCP connectors

## Examples of MCP tool descriptions

Here are just a few examples of well-written tool descriptions.

1. Google Drive’s update_drive tool: “Update shared drive settings including name, color, and restrictions.”

This tool description provides just enough context by sharing the specific items that can be updated in a shared drive.

See more available tools from a Google Drive MCP server.

2. Slack’s create_channel tool: “Create a new channel.”

Most tool descriptions are as simple as this. There’s just a verb (e.g., create) along with a resource from the MCP server (e.g., Slack’s channel resource).

See more available tools from a Slack MCP server.

3. Salesforce’s create_contact tool: “Create a new Salesforce contact. Required workflow: Call discover_required_fields('Contact') first to identify mandatory fields and prevent creation errors.”

To help agents invoke this tool at the appropriate point in a workflow, its description explicitly lays out the tool call that needs to precede it and why.

See more available tools from a Salesforce MCP server.

4. Linear’s get_comment tool: “Get a single comment by ID.”

This tool description specifies that it’ll only provide one comment (if the agent needs several comments, it’d need to call another tool). It also clarifies that it needs the comment’s ID in the arguments before it can fetch it.

See more available tools from a Linear MCP server.

5. Gong’s get_calls tool: “List ALL calls in date range - no user/workspace filtering. To filter by user/workspace, use search_calls_extensive instead.”

This tool description explicitly confirms that the agent needs to use a date range in its arguments. It also prevents the agent from invoking this tool if the user needs call recordings from a specific user or workspace (they can use the alternative that’s provided instead).

See more available tools from a Gong MCP server.

## Best practices for writing MCP tool descriptions

To help you write tool descriptions, you can follow these best practices.

Note: If you’re just evaluating MCP servers for your agents, you can use several of these best practices to evaluate the quality of their tools’ descriptions.

### Front load with important information

AI agents may not read the entire tool description, especially if it’s several sentences. With that in mind, include the most important information at the very beginning.

For example, instead of the following for Salesforce’s create_contact tool: “Before using this tool, make sure the user is authenticated with Salesforce. This tool creates a new contact record in Salesforce and returns the contact’s ID.”

You should structure it as, “Create a new Salesforce contact. Requires prior Salesforce authentication.”

The second part of this description also isn’t required, which leads us to our next best practice.

### Test tool descriptions through tool call evaluations

To help you determine whether a tool description is optimal or needs to be refined, you can use a solution like Merge Agent Handler to add sample prompts (what you think users might input) that should call a specific tool. You can also specify the MCP server you want to test, the user you’re testing on behalf of, the LLM the agent uses, and more.

You can see if the agent then calls the tool across your test prompts, and if it doesn’t, you can make adjustments to your description and re-test to see if you’ve improved its performance (i.e., improved the tool’s hit rate).

To help bring this to life, here’s how you can use Merge Agent Handler to test your tool descriptions (and your tools more broadly):

### Include tool-calling operations in the schema—not the description

The schema is the best place to include the metadata an agent needs to execute the call correctly, whether that’s authentication requirements, pagination behavior, filtering options, rate limits, and so on.
