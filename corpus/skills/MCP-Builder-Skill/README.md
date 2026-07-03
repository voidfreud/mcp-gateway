# MCP Builder Skill

A comprehensive skill for Claude Code Desktop that serves as the definitive guide for building production-ready MCP (Model Context Protocol) servers.

## Overview

The MCP Builder skill provides practical, actionable guidance for creating high-quality, maintainable MCP servers using both **FastMCP (Python)** and **MCP SDK (Node/TypeScript)**. This skill covers everything from fundamentals to production deployment, with real-world examples and best practices.

## Features

- 📚 **Comprehensive Coverage**: Complete guide covering all aspects of MCP server development
- 🔄 **Dual Implementation**: Equal depth coverage for both FastMCP (Python) and MCP SDK (TypeScript)
- ✅ **Best Practices**: Security, error handling, testing, and production readiness guidelines
- 💡 **Real-World Examples**: Complete working examples for GitHub API, Notion API, databases, and file operations
- 🎯 **Actionable Guidance**: No theoretical fluff—everything is immediately actionable
- 📋 **Quality Checklists**: Implementation quality validation checklists
- 🌳 **Decision Trees**: Architectural decision guidance

## Contents

The skill includes comprehensive coverage of:

1. **MCP Fundamentals** - Protocol architecture, transport layers, client-server communication patterns
2. **Tool Design Principles** - Atomic operations, input/output contracts, parameter validation
3. **Implementation Patterns** - REST APIs, databases, file systems, authentication flows
4. **Error Handling** - Explicit error types, retry logic, timeout handling, graceful degradation
5. **Testing Strategy** - Unit tests, integration tests, schema validation, authentication testing
6. **Security Best Practices** - API key management, input sanitization, rate limiting
7. **Documentation Standards** - LLM-friendly descriptions, parameter documentation, usage examples
8. **Production Readiness** - Configuration management, deployment, monitoring, versioning
9. **Common Pitfalls** - Anti-patterns to avoid, performance bottlenecks, common bugs
10. **Real-World Examples** - Complete working examples for common use cases

## Installation

### For Claude Code Desktop

1. Clone or download this repository
2. Copy the `mcp-builder-skill.md` file to your Claude skills directory:
   ```bash
   mkdir -p ~/.claude/skills/mcp-builder-skill
   cp mcp-builder-skill.md ~/.claude/skills/mcp-builder-skill/SKILL.md
   ```
3. Open Claude Code Desktop
4. Navigate to **Settings** → **Capabilities** → **Skills**
5. Find **"MCP Builder"** in the list and toggle it **ON**

### Alternative: Upload as ZIP

1. Create a directory named `mcp-builder-skill`
2. Copy `mcp-builder-skill.md` into it and rename to `SKILL.md`
3. Compress the directory into `mcp-builder-skill.zip`
4. In Claude Code Desktop: **Settings** → **Capabilities** → **Skills** → **Upload skill**

## Usage

Once installed and enabled, you can use the skill by asking Claude questions like:

- "Help me build an MCP server for GitHub API integration"
- "Show me how to implement proper error handling in FastMCP"
- "What are the best practices for tool design in MCP servers?"
- "Create a production-ready MCP server for [your API]"
- "How do I handle rate limiting in my MCP server?"

The skill will automatically provide comprehensive guidance based on your query.

## What You'll Learn

After using this skill, you'll be able to:

- ✅ Generate production-ready MCP servers from API specifications
- ✅ Make intelligent decisions about tool granularity and structure
- ✅ Implement proper error handling and validation automatically
- ✅ Write comprehensive tests without prompting
- ✅ Follow security best practices by default
- ✅ Produce code that passes review without major revisions

## File Structure

```
MCP-Builder-Skill/
├── README.md                    # This file
├── mcp-builder-skill.md         # The main skill file (rename to SKILL.md for installation)
└── DEPLOYMENT_COMPLETE.md       # Deployment instructions and troubleshooting
```

## Requirements

- Claude Code Desktop (for using the skill)
- Python 3.11+ (for FastMCP examples)
- Node.js 20+ (for MCP SDK examples)
- Basic understanding of async programming

## Examples Covered

The skill includes complete working examples for:

- **GitHub API Integration** - Creating and listing issues, pull requests
- **Notion API Integration** - Creating pages, managing databases
- **Database Connections** - PostgreSQL with connection pooling
- **File System Operations** - Safe file reading, writing, directory listing
- **REST API Integration** - Proper async/await patterns
- **Authentication Flows** - Token management, OAuth patterns

## Best Practices Included

- 🔒 Security: API key management, input sanitization, SQL injection prevention
- ⚡ Performance: Connection pooling, async patterns, resource cleanup
- 🛡️ Error Handling: Explicit error types, retry logic, graceful degradation
- 📝 Documentation: LLM-friendly descriptions, comprehensive examples
- 🧪 Testing: Unit tests, integration tests, schema validation
- 🚀 Production: Configuration management, monitoring, versioning

## Contributing

Contributions are welcome! If you have improvements, additional examples, or corrections, please feel free to submit a pull request.

## Resources

- [FastMCP Documentation](https://github.com/jlowin/fastmcp)
- [MCP SDK Documentation](https://modelcontextprotocol.io)
- [MCP Specification](https://spec.modelcontextprotocol.io)

## License

This skill is provided as-is for educational and development purposes.

## Support

For issues, questions, or suggestions, please open an issue on the [GitHub repository](https://github.com/EduardoRemedios/MCP-Builder-Skill).

---

**Version**: 1.0.0  
**Last Updated**: November 2024

