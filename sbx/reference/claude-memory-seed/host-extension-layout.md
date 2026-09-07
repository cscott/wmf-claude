---
name: host-extension-layout
description: "How cscott lays out MediaWiki checkouts on the host, and the lowercase `extensions` symlink that makes phan's relative paths work"
metadata: 
  node_type: memory
  type: user
  originSessionId: ee1a7641-c07b-449a-884c-2304ba223aed
  modified: 2026-09-17T23:14:38.590Z
---

On the host, MediaWiki extensions live in a `Wikimedia/Extensions/`
directory, with a lowercase `extensions` symlink beside it pointing at it.
That symlink is what makes the `../../extensions/<Name>` paths hard-coded
in every `.phan/config.php` resolve locally. A few repos are checked out
somewhere else for historical reasons (in the `sbx-translate` sandbox,
`UniversalLanguageSelector` sits beside `Extensions/`, not inside it), and
for those the trick fails, so phan's missing-sibling errors bite from time
to time.

A wmf-sbx sandbox mirrors the repo paths but not the symlink between them,
so the sandbox never gets the trick. See the "use the dependency set WMF CI
uses" to-do in `sbx/NOTES.md` for the options. Related:
[[wmf-sbx-testing-work]].
