# Third-party notices

`core/skills/` contains skills copied verbatim from the open-source projects below, at the commits listed in `core/MANIFEST.tsv`. They keep their original licences. `scripts/fetch_vendor.sh` clones the same upstream repositories into `third_party/` (not part of this repository).

| Skills | Upstream | Licence | Copyright |
|---|---|---|---|
| 45 research skills + `shared-references/` | [wanshuiyin/Auto-claude-code-research-in-sleep](https://github.com/wanshuiyin/Auto-claude-code-research-in-sleep) @ 26b95cfa0d87 | MIT | Copyright (c) 2026 wanshuiyin |
| 12 scientific skills | [K-Dense-AI/scientific-agent-skills](https://github.com/K-Dense-AI/scientific-agent-skills) @ a1043ef544e3 | MIT | Copyright (c) 2025 K-Dense Inc. |
| `find-skills` | [vercel-labs/skills](https://github.com/vercel-labs/skills) @ 13e4063a1cf9 | MIT | Copyright (c) 2026 Vercel, Inc. |
| `skill-creator` | [anthropics/skills](https://github.com/anthropics/skills) @ dbd4588f9e10 | Apache License 2.0 (full text in `core/skills/skill-creator/LICENSE.txt`) | Anthropic, PBC |
| `humanizer` | [blader/humanizer](https://github.com/blader/humanizer) v3.1.0 | MIT (`core/skills/humanizer/LICENSE`) | Copyright (c) 2025 Siqi Chen |
| `research-paper-writing` | upstream repository not recorded | MIT (`core/skills/research-paper-writing/LICENSE`) | Copyright (c) 2026 Master-cai |

The MIT licence text that applies to the MIT-licensed components above (with the respective copyright line):

```
MIT License

Permission is hereby granted, free of charge, to any person obtaining a copy
of this software and associated documentation files (the "Software"), to deal
in the Software without restriction, including without limitation the rights
to use, copy, modify, merge, publish, distribute, sublicense, and/or sell
copies of the Software, and to permit persons to whom the Software is
furnished to do so, subject to the following conditions:

The above copyright notice and this permission notice shall be included in all
copies or substantial portions of the Software.

THE SOFTWARE IS PROVIDED "AS IS", WITHOUT WARRANTY OF ANY KIND, EXPRESS OR
IMPLIED, INCLUDING BUT NOT LIMITED TO THE WARRANTIES OF MERCHANTABILITY,
FITNESS FOR A PARTICULAR PURPOSE AND NONINFRINGEMENT. IN NO EVENT SHALL THE
AUTHORS OR COPYRIGHT HOLDERS BE LIABLE FOR ANY CLAIM, DAMAGES OR OTHER
LIABILITY, WHETHER IN AN ACTION OF CONTRACT, TORT OR OTHERWISE, ARISING FROM,
OUT OF OR IN CONNECTION WITH THE SOFTWARE OR THE USE OR OTHER DEALINGS IN THE
SOFTWARE.
```

Runtime dependency fetched at install time (not redistributed): [pypdf](https://github.com/py-pdf/pypdf) (BSD-3-Clause), installed into `third_party/pylib` by `scripts/fetch_vendor.sh`.
