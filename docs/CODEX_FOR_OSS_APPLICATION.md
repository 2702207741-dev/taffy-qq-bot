# Codex for Open Source application worksheet

This worksheet is deliberately factual. It is a preparation aid, not evidence that OpenAI will select the project. The official documentation checked on 2026-09-21 says applicants should be a "core maintainer" or "run a widely used public project", that the award is capped at "six months of ChatGPT Pro with Codex", that extended Codex Security access requires write access and is evaluated "case by case", and that a repository without mainstream visibility is asked to "explain why" it matters to the broader ecosystem. No star, contributor or usage threshold is published, and submitting is not a claim of eligibility. See the official [program page](https://developers.openai.com/community/codex-for-oss) and [terms](https://learn.chatgpt.com/docs/codex-for-oss-terms). The `openai.com/.../form/codex-for-oss/` page returns HTTP 403 to automated retrieval, so confirm the live form fields by opening it in a browser before submitting.

## Publish first

Before applying, create a public GitHub repository and complete every item in [RELEASE_CHECKLIST.md](RELEASE_CHECKLIST.md). Make the maintainer's GitHub profile public and make sure its account visibly owns or has write access to the repository. Do not submit confidential material: the program terms state that application materials are not confidential.

## Form fields

### Maintainer role

Choose **Primary maintainer** only if you actually own or lead releases, issue triage, reviews, and roadmap decisions for this repository. Otherwise choose **Core maintainer** only if you have sustained write access and ongoing maintenance responsibility.

### Why the repository qualifies (maximum 500 characters)

Use only verified current metrics. Replace every bracket before submitting:

> Taffy is a self-hosted CS2 assistant for QQ communities, built on NoneBot2/OneBot V11. It provides local state, scoped memory, market queries and an admin console. I maintain [actual duties]. Public evidence: [repository URL], [releases/PRs/issues], [verified adoption]. Codex would support regression tests, security review and maintenance automation. This is a newly published project; I do not claim established adoption without evidence.

The template is not submission-ready: replace brackets with accurate facts and recount the final answer to keep it within 500 characters. Never leave placeholder metrics in the submitted form.

Do not substitute aspirations, local development activity, or unverified user counts for public evidence. A new repository can still be submitted, but its selection odds are inherently harder to demonstrate than those of an established, actively used project.

### Engineering evidence you may quote as-is

These are reproducible properties of this repository, not adoption metrics. State them only for the commit you publish, and link the CI run that shows them. They do not substitute for usage evidence, and "no known advisories" is not "no vulnerabilities":

- Supported interpreters `>=3.12,<3.15`; the isolated suite passes on Linux for 3.12.3, 3.13.13 and 3.14.6 (2,014 tests each) and on Windows for 3.12 (2,013 passed, 1 skipped).
- Ruff lint, Ruff format check, and mypy are clean for the main package and the desktop studio; both dependency sets audit clean against OSV at the locked versions.
- GitHub Actions are pinned to commit SHAs rather than mutable tags; secret scanning covers the working tree, reachable history, and the built release archive in CI.
- A release-archive verifier checks committed-file identity, missing/extra/duplicate members, unsafe paths, symlinks, Windows path aliases and credential patterns.

Anything the reader cannot verify from the public repository or a CI run must stay in brackets or be left out.

### What only you can supply

| Field | Evidence you must provide |
| --- | --- |
| Repository URL | The published public repository, matching the README badge and clone URL. |
| Maintainer identity | The GitHub account that visibly owns or holds write access, plus your ChatGPT-account email. |
| Adoption | Real stars, forks, issues, PRs, releases, or downstream users — none of which exist yet for a new repository. |
| Organisation ID | The actual OpenAI platform organisation ID from your dashboard. |
| Impact claim | A concrete reason the project matters to its community, phrased from experience rather than ambition. |

### API-credit use (maximum 500 characters)

> 我会把 API 额度仅用于本仓库的维护工作流：对已脱敏的 issue 分类；依据失败 CI 生成可审查的复现与修复建议；汇总 PR 的风险和测试缺口；从已合并 diff 起草发布说明。最终审查、合并和发布始终由人类维护者完成。不会上传 `.env`、生产数据库、聊天原文、用户身份信息，也不会扫描无权管理的仓库或系统。

### Other details (maximum 500 characters)

> 塔菲的公开发行版将维护安全披露政策、贡献规范、数据源政策、隐私说明、锁定依赖、GitHub Actions、历史密钥扫描和独立测试。它面向自托管的中文 CS2 QQ 社群；市场数据存在延迟或缺失时会明确标注，不把模型输出伪装成事实或投资建议。

## Final accuracy gate

- Use the ChatGPT-account email required by the form.
- Use the actual public GitHub username and public repository URL.
- Provide the actual OpenAI organization ID from the Platform dashboard.
- Request Codex Security only for repositories you own, maintain, or are authorised to administer.
- Keep a copy of the exact answers submitted and update them if OpenAI requests verification.
