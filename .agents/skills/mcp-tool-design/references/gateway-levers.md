# Gateway levers

Read this before proposing a gateway change. It describes the current project
surface; it does not authorize a write.

## Prefer the narrowest lever

- Use per-backend instructions for cross-tool guidance.
- Use a tool override for name, title, description, enablement, and parameter
  presentation. For client-specific discovery or result metadata, select and
  follow that client's profile before proposing an override.
- Use resource and prompt overrides for their own advertised text and
  enablement. Preserve resource URIs and original backend identities.
- Treat `validate` and `post_process` as arbitrary daemon code, not wording.
  Propose them only as separately scoped, explicitly authorized work.

## Preserve boundaries

- Keep an override tied to the captured original identity and use reset or
  migration facilities when an upstream tool changes.
- Avoid backend enablement, backend changes, virtual tools, and endpoint work;
  those change topology or behavior and are outside this skill.
- Inspect the documented configuration and API contract before relying on a
  field, byte limit, hot-reload result, or validation rule.

## Project sources

- [Configuration](../../../../docs/configuration.md)
- [Admin guide](../../../../docs/admin-guide.md)
- [HTTP API](../../../../docs/api.md)
