# MCP Schema Design: A Practical Guide (2026)

> Source: https://yaw.sh/mcp-in-production/mcp-schema-design

# MCP Schema Design: A Practical Guide (2026) - yaw
URL: https://yaw.sh/mcp-in-production/mcp-schema-design
Published: 2026-05-23

An MCP server's`tools/list` output is the API the model reads to decide what to call. It is also the API the model reads every turn. A schema that takes 40,000 tokens to describe is a schema that costs you 40,000 tokens every turn -- and at that size the model demonstrably degrades on selection: it grabs adjacent tools, conflates similarly-named ones, or refuses to use the server at all.

This guide covers the patterns that keep a tool surface scannable: naming, parameter modeling, the size budget, and when to split a server in two. The full treatment is Chapter 5 of MCP in Production-- the longest chapter in the book, for reasons that will become obvious.

## Naming: verb-noun, always

The model parses tool names as instructions.`list_orders`,`get_customer`,`refund_subscription`. Each one is a verb the model understands acting on a noun the model can identify.

Anti-patterns:

- `orders`-- noun only. The model has to infer the action from context. Wrong choice rate goes up.
- `do_thing_with_orders`-- verb is vague. The model picks it when nothing else matches.
- `OrderList`-- camel case. Mixes poorly with snake-case tools and the model treats the boundary as semantic.
- `list_orders_v2`-- versions in names. The model treats v2 and v1 as different concepts; you have created two tools instead of one.

## The description is for the model, not the human reader

The description is the firing predicate. "List orders" is a label; "List orders for the authenticated customer, optionally filtered by status and date range. Use when the user asks about their order history." is an instruction.

Three things every description should answer:

1. What does it do?
2. When should the model use it?
3. What does it return? (especially if the return shape is non-obvious)

Skip the marketing tone. Skip the apologies ("This is a basic tool..."). Tell the model when to fire.

## Parameters: constrain on purpose

Every unconstrained parameter is a coin flip.`{status: string}` with no enum produces "active", "Active", "ACTIVE", "open", and "in_progress" depending on the model's context. Constrain:

```
{
 "type": "object",
 "properties": {
 "status": {
 "type": "string",
 "enum": ["pending", "paid", "refunded", "cancelled"],
 "description": "Order status filter. Omit to return orders in any status."
 },
 "limit": {
 "type": "integer",
 "minimum": 1,
 "maximum": 100,
 "default": 25
 }
 }
}
```

Enums save the model from guessing. Defaults reduce the surface the model has to think about. Required fields force the model to gather the right context before calling.

## The 40,000-token tool list problem

The`aws-mcp` server in the @yawlabs portfolio started small. By the time it covered a useful subset of the AWS API it had ~200 tools, and the rendered`tools/list` output was ~40,000 tokens. At that size:

- The model picked adjacent tools (`ec2_describe_instances` when asked for`ec2_describe_volumes`) more often than at 20 tools.
- Tool-selection latency went up as the model spent more context comparing.
- Every single turn paid the 40,000-token tax before the user's question was processed.

Three patterns reduce the surface:

- Profile-based loading. Ship a server that exposes a subset of tools based on a startup flag (`--profile=read-only`,`--profile=ec2`). Most workflows don't need everything; let the user opt in.
- Split the server. One`@yawlabs/aws-ec2-mcp`+`@yawlabs/aws-s3-mcp` beats one mega-server. The user loads only what their workflow needs.
- Collapse synonyms. Three tools that all start a workflow with a slight variation become one tool with an enum parameter.

## Idempotency: the write-tool discipline

The model retries. Sometimes it retries because a previous call timed out; sometimes because the response shape was confusing; sometimes because the user re-prompted. Every write tool should be idempotent by default -- accept an`idempotency_key` parameter, or use natural keys (an email, a slug) the second call would dedupe on.

A`create_customer` that creates a duplicate customer on retry is a tool that produces support tickets.

## Cross-server composition

The model often chains tools across servers: get a customer with one server, list their orders with another, refund one with a third. The output of A becomes the input of B. Make outputs that compose: use the same ID format your input parameters expect, return enough context (not just IDs) that the model can reason about what it has, paginate explicitly so the model knows when there's more.

## Want the full chapter?

MCP in Production Chapter 5 is the longest in the book. It covers the full naming pattern with anti-patterns from real servers, the description pattern with measured comparisons, parameter modeling with worked examples, the size-budget heuristics, the profile-and-split patterns for large servers, and the cross-server composition rules.

MCP in Production

The MCP server book. Twelve chapters from shipping fourteen @yawlabs/* servers. PDF + EPUB. Free updates as the spec moves. Free with a Token Limit News signup.

Read more & get it free →

Published by Yaw Labs.

## Related

- MCP in Production-- the book.
- Build an MCP Server: A Practical Tutorial
- MCP Server Auth
- MCP Server Testing
- Grading MCP Servers: 88 Tests-- the eval suite that surfaces schema problems.
