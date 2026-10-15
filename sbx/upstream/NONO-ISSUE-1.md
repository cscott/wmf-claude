# nono proxy: the OAuth capture store is read once, written whole

For: https://github.com/always-further/nono/issues (a feature request or
bug; the engineer files it). Read against nono `main` 45fcc24
(`crates/nono-proxy/src/oauth_capture/mod.rs`, `persist.rs`) and seen
with nono 0.79.

## What happens

Several `nono proxy` processes can share one capture store: the same
`XDG_STATE_HOME`, so the same `oauth-capture/providers.json`. Each
process reads the file once, in `OAuthCaptureStore::load_with_persistence`,
and then resolves phantoms only from its in-memory map. On each capture,
`store_phantom` writes its whole in-memory map back to the file
(`persist_tokens`).

So:

1. A phantom that proxy A mints is not resolvable in proxy B, if B
   started before the capture. B forwards the phantom unchanged: Claude
   Code gets a 401 and its refresh gets a 400 ("OAuth session expired
   and could not be refreshed").
2. When B then captures, it writes its map, and A's newer phantoms are
   gone from the file (a lost update). A works until it restarts.
3. Two processes that persist at the same time race on
   `providers.json.tmp` (`remove_file`, then `create_new`).

## Use case

wmf-sbx runs one `nono proxy` per VM sandbox, as each sandbox's egress
proxy, and wants one Claude login for all of them: `/login` in one
sandbox, and the others use it. Today it restarts a proxy whose start
is older than the store file's mtime before giving its sandbox new
phantoms. That works, but the restart and the lost update should not be
needed.

## Proposal

- On a phantom miss in `resolve`, read the file again if its mtime
  changed since the last read, and merge.
- In `persist_tokens`, read the file and merge it with the in-memory map
  before the write, under an advisory lock (`flock` on the directory or
  a lock file), and use a unique temporary name.
