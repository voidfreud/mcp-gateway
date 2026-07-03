# MCP: Tool Design

> Source: https://llmbestpractices.com/ai-agents/mcp-tool-design

# MCP: Tool Design · llmbestpractices
URL: https://llmbestpractices.com/ai-agents/mcp-tool-design
Published: 2026-06-17

## Overview

An MCP tool description is part of the model’s context window. The model reads the name, description, and JSON Schema before deciding whether and how to call a tool. Tool design mistakes surface as model errors: wrong arguments, skipped required fields, calling a tool when a resource is correct. The rules on this page reduce those failures by making tools unambiguous, narrow, and self-documenting.

## Name tools with verbObject in snake_case

Tool names are the first signal the model uses to pick a tool. Use a verb followed by an object noun in`snake_case`:`search_issues`,`create_pull_request`,`delete_record`. Avoid noun-first names (`issues_search`), vague verbs (`process_thing`), and acronyms that require domain knowledge.

Good names read as natural English imperatives. When the model sees`search_issues`, it knows the tool searches and the thing it searches is issues. When it sees`do_stuff`, it does not.

Reserve the most important real estate, the verb, for the action. The noun narrows the domain. Together they leave no ambiguity about when the tool applies.

## Use narrow types and enums; avoid bare strings

A tool parameter typed`string` accepts anything. A parameter typed as an enum or with a defined pattern accepts only valid values. Narrow types catch model errors before they reach your API.

```
{
  "state": { "type": "string", "enum": ["open", "closed", "all"] },
  "priority": { "type": "integer", "minimum": 1, "maximum": 5 },
  "label": { "type": "string", "pattern": "^[a-z][a-z0-9-]*$" }
}
```

When a parameter has a small, known set of valid values, always use an enum. The model will pick from the listed values rather than hallucinate a string. When a parameter has a numeric bound, declare`minimum` and`maximum` so the model stays in range without guessing.

## Put an example invocation in the description field

The`description` field is read by the model at tool-selection time. Include: what the tool does, what it returns, and one concrete example invocation. The example is the most valuable line because it shows the model the exact argument shape.

```
{
  "name": "search_issues",
  "description": "Search GitHub issues by query string. Returns up to 30 issues sorted by relevance with id, title, state, and labels. Example: search_issues({ \"q\": \"is:open label:bug repo:org/repo\" })."
}
```

Descriptions that omit examples force the model to infer argument shape from the JSON Schema alone. Schemas are precise but not intuitive. Examples are intuitive. Use both.

## One tool per concern; resist the temptation to merge operations

A`manage_issues` tool that creates, updates, and deletes issues based on an`action` parameter is harder for the model to call correctly than three narrow tools. The multi-action pattern pushes disambiguation onto a string enum inside the call, which the model must get right every time.

Split tools by operation, not by domain object.`create_issue`,`update_issue`,`close_issue` are three tools. Each has a clear purpose, a clear required-fields set, and a clear success state. The model makes fewer mistakes because each path is unambiguous.

The exception: batched reads. A single`list_issues` tool with optional filters is better than`list_open_issues`,`list_closed_issues`,`list_issues_by_label`. Filter-based reads are compositional; action-based writes are not.

## Mark exactly the fields the tool cannot infer as required

Over-specifying required fields forces the model to supply values it does not have, leading to hallucinated arguments. Under-specifying required fields leads to missing arguments and runtime errors.

A field is required when the tool cannot default or infer it. A search query`q` is required because there is no sensible default. A`limit` parameter is not required if the tool has a safe default of 30.

```
{
  "required": ["q"],
  "properties": {
    "q": { "type": "string" },
    "limit": { "type": "integer", "minimum": 1, "maximum": 100, "default": 30 }
  }
}
```

Sane defaults reduce model error. The model will supply a required field if it can; it will not always supply an optional one.

## Avoid tool sprawl; remove tools the model no longer needs

Each tool added to the server adds tokens to every tool-selection prompt. At some point the list becomes too long for the model to navigate reliably and error rates rise. See mcp-servers for the rule: scope one server per domain, not per tool.

Audit the tool list after each iteration. Tools that have never been called in production are candidates for removal. Tools that are always called together are candidates for merger. mcp-logging provides the usage data needed to make this call.

## Related
