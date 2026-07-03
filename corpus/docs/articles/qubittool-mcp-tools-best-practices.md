# The Art of AI Agent Tools: Best Practices for Writing High-Quality MCP Tools

> Source: https://qubittool.com/blog/mcp-tools-best-practices-ai-agent

# The Art of AI Agent Tools: Best Practices for Writing High-Quality MCP Tools and Function Definitions | QubitTool
URL: https://qubittool.com/blog/mcp-tools-best-practices-ai-agent
Published: 2026-04-23
Author: QubitTool

# The Art of AI Agent Tools: Best Practices for Writing High-Quality MCP Tools and Function Definitions

2026-04-23 - QubitTool Tech Team

Most guides on building AI agents focus on prompt design, orchestration frameworks, or model selection. They rarely spend time on the part that determines whether an agent actually works in production: the quality of its tools.

A tool is the interface between an LLM and the real world. When that interface is vague, the model guesses. When it is brittle, the agent fails silently. When it is well-crafted, the agent becomes reliable, predictable, and genuinely useful.

This article is a practitioner's guide to writing tools that LLMs can use correctly. Whether you are building MCP Server tools, OpenAI function calling definitions, or any other tool use integration, the principles here apply universally.

If you are new to the MCP protocol, start with the MCP Protocol Deep Dive. If you already have a working MCP Server and want to make your tools production-grade, keep reading.

## Key Takeaways

- Tool design has a larger impact on agent reliability than prompt design
- Every tool needs five components: a precise name, a rich description, a strict input schema, structured error handling, and predictable output
- 10 best practices cover naming, atomicity, error messages, idempotency, validation, output format, rate limiting, timeouts, versioning, and documentation
- Anti-patterns like "god tools," silent failures, and unstructured outputs cause the majority of agent errors in production
- Test tools with real LLMs, not just unit tests — the model is the ultimate consumer

## Why Tool Design Matters More Than Prompt Design

Consider a common agentic workflow: a user asks an agent to "find all overdue invoices and send a reminder email to each customer." The agent must select the right tools, construct valid arguments, interpret results, and chain operations together. If any tool in that chain has an ambiguous description, the agent picks the wrong one. If a schema allows invalid input, the call fails at runtime. If an error message is opaque, the agent cannot recover.

You can write the most sophisticated prompt engineering system in the world, but if the tools are poorly defined, the agent will still fail. Prompts guide reasoning. Tools define capability. Quality tools reduce hallucination at the structural level by constraining what the model can do and clarifying when it should do it.

The 2025-03-26 MCP specification recognized this by introducing Tool Annotations— metadata fields like`readOnlyHint`,`destructiveHint`, and`idempotentHint` that help clients make smarter decisions about tool invocation. This is a protocol-level acknowledgment that tool metadata is as important as tool functionality.

## Anatomy of a Great Tool

Every well-designed tool, whether it is an MCP tool or a function calling definition, has five components:

### 1. Name

The name is the first thing the LLM sees when deciding which tool to call. It must be specific, unambiguous, and follow a consistent pattern.

```
// Bad: vague, could mean anything
{ "name": "process" }

// Bad: too generic, overlaps with other tools
{ "name": "get_data" }

// Good: specific verb + noun, clear scope
{ "name": "list_overdue_invoices" }

// Good: follows resource_action pattern
{ "name": "email_send_reminder" }
```

### 2. Description

The description is the most underestimated field in tool design. The LLM reads this to decide when to use the tool — it functions as prompt engineering embedded in the tool definition itself.

```
// Bad: restates the name
{
  "name": "search_documents",
  "description": "Searches documents"
}

// Good: explains what, when, and output
{
  "name": "search_documents",
  "description": "Full-text search across the company knowledge base. Use when the user asks to find internal documentation, policies, or technical specs. Returns up to 10 results ranked by relevance, each with title, snippet, and URL. Does NOT search emails or chat messages — use search_communications for those."
}
```

### 3. Input Schema

The input schema is your contract with the LLM. Defined in JSON Schema, it tells the model exactly what arguments are expected, what types they must be, and what constraints apply. A strict schema prevents malformed calls before they reach your handler.

```
// Bad: accepts anything, no constraints
{
  "inputSchema": {
    "type": "object",
    "properties": {
      "query": { "type": "string" }
    }
  }
}

// Good: constrained, documented, with defaults
{
  "inputSchema": {
    "type": "object",
    "properties": {
      "query": {
        "type": "string",
        "minLength": 1,
        "maxLength": 500,
        "description": "Natural language search query or exact document title"
      },
      "limit": {
        "type": "integer",
        "minimum": 1,
        "maximum": 50,
        "default": 10,
        "description": "Maximum number of results to return"
      },
      "category": {
        "type": "string",
        "enum": ["engineering", "hr", "finance", "legal", "all"],
        "default": "all",
        "description": "Restrict search to a specific document category"
      }
    },
    "required": ["query"],
    "additionalProperties": false
  }
}
```

### 4. Error Handling

When a tool fails, the LLM needs enough information to decide whether to retry, try a different tool, or ask the user for help. Opaque errors like`"Internal Server Error"` leave the model stranded.

```
// Bad: generic error, LLM cannot reason about what went wrong
return { error: "Something went wrong" };

// Good: structured error with actionable context
return {
  isError: true,
  content: [{
    type: "text",
    text: JSON.stringify({
      error: "RATE_LIMIT_EXCEEDED",
      message: "Search API rate limit reached. Maximum 10 requests per minute.",
      retryAfterSeconds: 30,
      suggestion: "Wait 30 seconds before retrying, or narrow the query to reduce result processing time."
    })
  }]
};
```

### 5. Output Format

Consistent, structured output allows the LLM to parse results reliably across invocations. Unpredictable output formats force the model to guess, which increases the chance of misinterpretation.

```
// Bad: inconsistent output shape
// Sometimes returns a string, sometimes an array, sometimes an object
return results.length > 0 ? results : "No results found";

// Good: consistent envelope, always the same shape
return {
  content: [{
    type: "text",
    text: JSON.stringify({
      status: "success",
      resultCount: results.length,
      results: results.map(r => ({
        title: r.title,
        snippet: r.snippet,
        url: r.url,
        relevanceScore: r.score
      })),
      hasMore: totalCount > results.length,
      nextCursor: results.length < totalCount ? lastId : null
    })
  }]
};
```

## 10 Best Practices for Writing High-Quality Tools

### 1. Use Clear, Consistent Naming

Adopt a naming convention and apply it across every tool on your server. Two patterns work well:

- verb_noun:`list_invoices`,`create_user`,`delete_file`
- resource_action:`invoice_list`,`user_create`,`file_delete`

Pick one and stick with it. Mixing conventions forces the LLM to learn two mental models.

### 2. Keep Tools Atomic

Each tool should do exactly one thing. A tool that "searches documents and sends them as an email attachment" is two operations fused together. When the search succeeds but the email fails, the agent cannot retry the email alone.

```
// Bad: compound tool, impossible to retry partially
{ name: "search_and_email_documents" }

// Good: two atomic tools the agent can compose
{ name: "search_documents" }
{ name: "send_email_with_attachments" }
```

Atomic tools are easier to test, easier for the LLM to reason about, and easier to compose into complex agentic workflows.

### 3. Write Rich, Actionable Error Messages

Error messages are prompts for the LLM. They should include:

- A machine-readable error code
- A human-readable explanation of what happened
- Whether the operation can be retried
- What the agent should do differently

See the error handling example in the anatomy section above. This pattern alone eliminates a large class of agent failures where the model receives a cryptic error and hallucinates a recovery path.

### 4. Make Tools Idempotent Where Possible

Idempotent tools produce the same result when called multiple times with the same arguments. This is critical because LLMs may retry calls due to perceived failures, timeouts, or multi-step reasoning loops.

```
// Bad: creates a duplicate record on every call
async function createInvoice({ customerId, amount }) {
  return db.invoices.insert({ customerId, amount, createdAt: new Date() });
}

// Good: uses idempotency key to prevent duplicates
async function createInvoice({ customerId, amount, idempotencyKey }) {
  const existing = await db.invoices.findOne({ idempotencyKey });
  if (existing) return { status: "already_exists", invoice: existing };
  return db.invoices.insert({ customerId, amount, idempotencyKey, createdAt: new Date() });
}
```

The MCP 2025-03-26 spec added`idempotentHint` to Tool Annotations for exactly this reason — to signal to clients which tools are safe to retry.

### 5. Validate Inputs Strictly

Never trust the LLM to send valid input. Models hallucinate parameter values, confuse types, and invent fields that do not exist. Your tool must validate every input before processing.

Use JSON Schema constraints (`minLength`,`maxLength`,`minimum`,`maximum`,`enum`,`pattern`) to catch errors at the protocol level. Then add application-level validation in your handler for business rules that JSON Schema cannot express.

```
server.tool(
  "transfer_funds",
  {
    fromAccount: z.string().regex(/^ACC-\d{8}$/, "Account ID must match ACC-XXXXXXXX format"),
    toAccount: z.string().regex(/^ACC-\d{8}$/),
    amount: z.number().positive().max(100000),
    currency: z.enum(["USD", "EUR", "GBP"]),
    idempotencyKey: z.string().uuid()
  },
  async (args) => {
    if (args.fromAccount === args.toAccount) {
      return errorResponse("INVALID_TRANSFER", "Source and destination accounts must differ.");
    }
    // proceed with transfer
  }
);
```

### 6. Return Structured, Predictable Output

Define a consistent output envelope and use it for every tool response. The LLM learns patterns — if your tools return different shapes depending on context, parsing failures multiply.

A proven pattern:

```
{
  "status": "success | error | partial",
  "data": { ... },
  "metadata": {
    "executionTimeMs": 142,
    "resultCount": 5,
    "hasMore": false
  }
}
```

For tools that return large datasets, include pagination cursors so the agent can request more results without re-executing the full query. This is especially important when working within context window constraints, where dumping thousands of rows into the conversation will degrade model performance.

### 7. Implement Rate Limiting and Backpressure

If your tool calls an external API, it inherits that API's rate limits. An agent in a reasoning loop can fire dozens of tool calls in seconds. Without rate limiting, your tool becomes a vector for API abuse.

Return rate limit information in error responses so the agent can wait intelligently:

```
if (isRateLimited) {
  return errorResponse(
    "RATE_LIMIT_EXCEEDED",
    `API rate limit reached. Retry after ${retryAfter} seconds.`,
    { retryAfterSeconds: retryAfter }
  );
}
```

### 8. Set Explicit Timeouts

Every tool that performs I/O should have a timeout. Without one, a hanging network request blocks the entire agent conversation. Set timeouts at the tool level and communicate them in the description.

```
async function fetchWebPage({ url }) {
  const controller = new AbortController();
  const timeout = setTimeout(() => controller.abort(), 10000);
  try {
    const response = await fetch(url, { signal: controller.signal });
    return { status: "success", content: await response.text() };
  } catch (err) {
    if (err.name === "AbortError") {
      return errorResponse("TIMEOUT", "Request timed out after 10 seconds. The target server may be slow or unreachable.");
    }
    throw err;
  } finally {
    clearTimeout(timeout);
  }
}
```

### 9. Plan for Versioning

Tools evolve. Parameters get added, behavior changes, schemas tighten. Without a versioning strategy, updates break existing agents silently.

Two approaches work:

- Semantic naming:`search_documents_v2` alongside`search_documents`(deprecated)
- Schema evolution: Add new optional fields, never remove or rename required fields, document changes in the tool description

For MCP Servers, prefer schema evolution. Adding a new tool version requires clients to update their tool selection prompts, while backward-compatible schema changes are transparent.

### 10. Document the Tool for the LLM

The`description` field is the tool's documentation. But for complex tools, the description alone is not enough. Use the`inputSchema` property descriptions to document each parameter. Include examples of valid values in the description text.

```
{
  "name": "query_database",
  "description": "Execute a read-only SQL query against the analytics database. Supports SELECT statements only. Maximum result size is 1000 rows. Use for answering questions about sales, inventory, or customer data. Example queries: 'SELECT customer_name, total_orders FROM customers WHERE region = 'APAC' ORDER BY total_orders DESC LIMIT 10'.",
  "inputSchema": {
    "type": "object",
    "properties": {
      "sql": {
        "type": "string",
        "description": "A valid SQL SELECT statement. Must not contain INSERT, UPDATE, DELETE, DROP, or ALTER. Limit results with LIMIT clause to avoid exceeding the 1000-row cap."
      }
    },
    "required": ["sql"]
  }
}
```

## Anti-Patterns to Avoid

These patterns cause the majority of tool-related failures in production agent systems.

### The God Tool

A single tool that accepts a`action` parameter and dispatches internally to dozens of different behaviors. The LLM cannot reason about a tool that does everything. It cannot predict the output format, it cannot provide the right arguments, and it cannot recover from errors because the error could mean anything.

```
// Anti-pattern: god tool
{
  "name": "manage_system",
  "inputSchema": {
    "properties": {
      "action": { "enum": ["create_user", "delete_user", "list_users", "update_settings", "restart_service", "query_logs"] },
      "payload": { "type": "object" }
    }
  }
}
```

Break this into six separate tools with dedicated schemas.

### Silent Failures

Tools that return`{ "result": null }` or an empty string when something goes wrong. The LLM interprets silence as success. It will confidently tell the user that the operation completed, when in fact nothing happened.

Always return an explicit error with`isError: true` in MCP, or a structured error object in function calling responses.

### Unstructured Blob Output

Returning a raw HTML page, an unparsed log file, or a multi-megabyte JSON dump as tool output. The model cannot extract useful information from unstructured blobs, and large outputs consume context window space that the agent needs for reasoning.

Parse, filter, and summarize before returning. If the full data is needed, return a reference (URL, ID) and provide a separate tool to fetch specific sections.

### Leaking Internal State

Returning database IDs, internal API keys, stack traces, or system paths in tool output. Beyond the security risk, internal identifiers confuse the model — it may attempt to use a database primary key as a user-facing identifier in its response.

### Overlapping Tool Descriptions

Two tools with descriptions so similar that the LLM cannot distinguish between them. For example,`search_documents`("Search for documents") and`find_files`("Find files in the system"). If the distinction matters, make it explicit in the description. If it does not, merge them into one tool.

## Testing Tools With LLMs

Unit tests verify that your tool handler works. They do not verify that an LLM can use your tool correctly. You need three layers of testing:

### Layer 1: Unit Tests

Test input validation, error handling, edge cases, and output format. These are standard software tests.

```
test("rejects transfer to same account", async () => {
  const result = await transferFunds({
    fromAccount: "ACC-00000001",
    toAccount: "ACC-00000001",
    amount: 100,
    currency: "USD",
    idempotencyKey: "550e8400-e29b-41d4-a716-446655440000"
  });
  expect(result.isError).toBe(true);
  expect(result.content[0].text).toContain("INVALID_TRANSFER");
});
```

### Layer 2: Protocol Integration Tests

For MCP tools, use the MCP Inspector to verify that your tool is discoverable, that its schema is correctly advertised, and that invocations over the protocol produce expected results. This catches serialization issues, transport bugs, and schema mismatches that unit tests miss.

If you have not set up a server yet, the MCP Server Quick Start Tutorial walks through the complete setup including Inspector integration.

### Layer 3: LLM-in-the-Loop Tests

Give your tools to a real model and evaluate whether it uses them correctly. Create a test suite of natural language prompts that should trigger each tool, and verify:

1. Tool selection: Did the model pick the right tool?
2. Argument construction: Are the arguments valid and sensible?
3. Result interpretation: Did the model correctly interpret the output?
4. Error recovery: When the tool returns an error, does the model retry appropriately or ask the user for clarification?

```
const testCases = [
  {
    prompt: "What were our top 5 customers by revenue last quarter?",
    expectedTool: "query_database",
    validateArgs: (args) => args.sql.includes("ORDER BY") && args.sql.includes("LIMIT"),
  },
  {
    prompt: "Send the quarterly report to the finance team",
    expectedTool: "send_email_with_attachments",
    validateArgs: (args) => args.recipients.length > 0,
  },
];
```

This layer catches the subtle failures that only emerge when a model interprets your tool definitions in context. For a deeper treatment of LLM testing strategies, see the AI Agent Development Guide.

## Tool Composition Patterns

Individual tools are building blocks. The real power emerges when an agent composes them into workflows. Design your tools to support these composition patterns:

### Sequential Pipeline

Tool A produces output that Tool B consumes. Ensure output formats align: if`search_documents` returns document IDs,`get_document_content` should accept those same IDs as input.

```
search_documents("overdue invoices")
  -> [doc_001, doc_002, doc_003]
    -> get_document_content("doc_001")
      -> extract_customer_email(content)
        -> send_email_with_attachments(email, reminder)
```

### Fan-Out / Fan-In

The agent calls the same tool multiple times with different arguments, then aggregates results. This works well when tools return consistent output shapes — the model can reason about the collection as a whole.

### Conditional Branching

The agent inspects tool output and selects the next tool based on conditions. This requires clear output status fields:`"status": "success"` vs`"status": "not_found"` vs`"status": "error"` lets the model branch reliably.

### Guard Tools

A lightweight tool that checks preconditions before the agent proceeds with a heavier operation. For example,`check_user_permissions` before`delete_database_record`. This pattern implements guardrails at the tool level rather than the prompt level, making them harder to bypass.

```
// Guard tool: fast, read-only, returns clear yes/no
server.tool(
  "check_user_permissions",
  {
    userId: z.string(),
    action: z.enum(["read", "write", "delete", "admin"]),
    resource: z.string()
  },
  async ({ userId, action, resource }) => {
    const allowed = await permissionService.check(userId, action, resource);
    return {
      content: [{
        type: "text",
        text: JSON.stringify({
          allowed,
          reason: allowed ? "User has the required permission." : `User lacks '${action}' permission on '${resource}'.`
        })
      }]
    };
  }
);
```

## From Function Calling to MCP: A Unified Perspective

If you are coming from the OpenAI function calling world, the principles in this guide translate directly. The difference is in the delivery mechanism: function calling defines tools inline in API requests, while MCP tools are registered on a server and discovered dynamically by any compatible client.

The LLM Function Calling Guide covers the OpenAI-specific implementation in detail. The Advanced MCP Protocol Practice shows how to build enterprise MCP Servers with authentication and streaming. The principles of good tool design — clear naming, strict schemas, rich descriptions, structured errors — transcend the delivery mechanism.

## Checklist: Before You Ship a Tool

Use this checklist before deploying any tool to production:

- Name follows the project's naming convention and is unambiguous
- Description explains what, when, and output format in 2-4 sentences
- Input schema has types, constraints, descriptions, and required fields;`additionalProperties` is`false`
- Output uses a consistent envelope with status, data, and metadata
- Errors return structured objects with error codes, messages, and recovery suggestions
- Idempotency: mutation tools accept an idempotency key or are naturally idempotent
- Validation rejects invalid input at both schema and application levels
- Timeouts are set for all I/O operations
- Rate limits are enforced and communicated in error responses
- No leaking of internal state, credentials, or stack traces in output
- Unit tests cover validation, error paths, and edge cases
- LLM test confirms the model selects and invokes the tool correctly

## Conclusion

The best AI agent systems are not the ones with the cleverest prompts or the largest models. They are the ones where the tools are so well-designed that the model barely has to think about how to use them.

Good tool design is a force multiplier. It reduces hallucination, eliminates retry loops, enables reliable composition, and makes your agent predictable enough to trust with real tasks. Invest in your tool definitions with the same rigor you invest in your APIs — because for an LLM, your tools are the API.

Start with the anatomy. Apply the ten practices. Avoid the anti-patterns. Test with a real model. And remember: every minute you spend improving a tool description saves hours of debugging mysterious agent failures in production.

## Further Reading

- MCP Protocol Deep Dive— Foundational understanding of the Model Context Protocol
- Build Your First MCP Server— Hands-on tutorial for creating tools, resources, and prompts
- MCP 2025-03-26 Spec Breakdown— Tool Annotations, OAuth, and the latest protocol features
- LLM Function Calling Guide— OpenAI function calling patterns and JSON Schema fundamentals
- AI Agent Development Guide— End-to-end guide to building production agent systems
- Advanced MCP Protocol Practice— Enterprise architecture with authentication and streaming
