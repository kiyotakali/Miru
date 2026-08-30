# Third-Party Notices

Miru's own source code is licensed under the Apache License 2.0. Miru also
integrates third-party libraries and assets which remain under their own
licenses and are not covered by Miru's Apache-2.0 license.

## Live2D Cubism

The released desktop and Android applications use Live2D Cubism components
and the Hiyori Momose sample model. The following files are intentionally not
included in Miru's public source archive:

- Live2D Cubism Core for Web
- Live2D Cubism Core for Native
- Live2D Cubism Native Framework
- Hiyori Momose sample model data

Developers who want to build the desktop pet or Android Live2D renderer must
obtain the matching components from Live2D and accept the applicable terms:

- Live2D Open Software License Agreement
- Live2D Proprietary Software License Agreement
- Live2D Free Material License Agreement
- Terms of Use for Live2D Cubism Sample Data

See `docs/BUILDING.md` for the required local paths. Do not assume that Miru's
Apache-2.0 license grants rights to redistribute these components or assets.

## Other dependencies

Python, Rust, Node, Android, and bundled JavaScript dependencies retain the
licenses declared by their upstream projects. Dependency manifests are kept in
the repository so their exact license texts can be inspected upstream.
