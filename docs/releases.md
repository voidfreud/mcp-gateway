# Releases

mcp-gateway is installed from this repository, not from a package index. A
release is a git tag `vX.Y.Z` on `main`, following semantic versioning; the
`v2.0.0` tag marks the end of the reset described in `PLAN.md`.

## Installing a release

```bash
uv tool install git+https://github.com/voidfreud/mcp-gateway            # latest main
uv tool install "git+https://github.com/voidfreud/mcp-gateway@v2.0.0"   # a tag
uv tool upgrade mcp-local-gateway                                        # move an unpinned install forward
```

The tool environment is named after the package, `mcp-local-gateway`; the
command stays `mcp-gateway`. Package installation never touches the gateway's
config, captured state, logs, or backups.

## Cutting a release

1. `just check` is green on `main`.
2. Tag: `git tag -a vX.Y.Z -m "vX.Y.Z" && git push origin vX.Y.Z`.

Nothing is published anywhere else. There is no release automation to run.
