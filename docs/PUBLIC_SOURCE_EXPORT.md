# Public Source Export

The cross-platform release binaries were built from runtime commit
`c803f22150431f4c57bf226ded268c9abbfae343`.

Miru's public repository is populated from that runtime tree with
`scripts/export_public_source.sh`. The export deliberately excludes:

- private maintainer notes and production deployment helpers;
- live smoke tests that require private infrastructure;
- Live2D Cubism Core, Framework, and Hiyori sample files governed by separate
  Live2D licenses.

The export does not alter Miru runtime code. Public-source-only changes are
limited to documentation, package metadata, secret scanning, and the exclusion
of separately licensed dependencies.

To produce a clean snapshot:

```bash
mkdir -p /tmp/miru-public-source
bash scripts/export_public_source.sh HEAD /tmp/miru-public-source
```

Before publishing, run the source scanner from the exported directory and
confirm that `.miru-runtime-source-ref` contains the intended immutable commit.
