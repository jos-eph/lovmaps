# AI Assistant Directives for this Repository

Welcome to the repository! If you are an AI assistant, coding agent, or automated tool interacting with this codebase, you **MUST** strictly adhere strictly to the following rules. This is an open-source, nonprofit repository with specific operational and cost-related constraints.

Check the CLAUDE_REFERENCE directory for instructions before asking the user or searching further.

## 🚨 CRITICAL Git & Permission Restrictions

1. **Human Approval Required:** All code merged into the `main` branch MUST be explicitly approved and merged by the human owner, `@jos-eph`. Do not attempt to bypass pull request reviews or auto-merge code.
2. **Pushing Code:** ONLY the GitHub user `@jos-eph` is authorized to push to this repository. 
    * **Agent Action:** You may generate code, draft commit messages, and provide step-by-step Git commands (e.g., `git checkout -b`, `git commit -m`), but you must **pause and wait** for `@jos-eph` to execute the push. Do not attempt to push directly using APIs or automated scripts.
3. **Branch Protection:** NEVER push directly to the `main` branch. All work must be done on feature branches.
4. **Branch Retention:** NEVER delete a branch, locally or remotely. Even if a branch has been merged, leave branch management and cleanup entirely to `@jos-eph`.
5. **Branch Safety:** Agents are ONLY allowed to make changes or add information on a branch with the suffix _agent_permitted. If the branch does not exist, the agent should create it.
## 💰 Cost Constraints & Nonprofit Rules

This is a nonprofit project. You must NEVER suggest, implement, or execute anything that could incur charges on GitHub.
* **GitHub Actions:** Optimize workflows to run quickly and efficiently within the GitHub Free tier limits. Avoid long-polling, infinite loops, or unnecessarily heavy compute matrices.
* **Storage Limits:** Do not suggest using paid storage solutions or exceeding GitHub's free limits for packages, LFS (Large File Storage), or artifacts.

## 🏗️ Data Pipeline & Architecture Guidelines

The planned architecture for this repository involves an agentic flow to download data, process it via cloud runners (GitHub Actions), and release data products using GitHub Releases. 

When working on scripts or workflows related to this pipeline, follow these safety guidelines:
* **Data Handling:** Ensure downloaded data is processed dynamically or stored in temporary runner memory (`/tmp`). Do not commit large datasets to the Git tree.
* **GitHub Releases:** Focus data output strategies entirely around GitHub Releases. Ensure that any drafted workflows use standard, free-tier API calls to upload assets to a Release rather than storing them as standard workflow artifacts (which consume storage quotas).
* **Rate Limits:** When writing scripts that fetch data from external sources or the GitHub API, always implement polite polling, backoffs, and rate-limit handling to prevent failing workflows and wasted runner minutes.
* **Idempotency:** Write data-fetching and processing scripts to be idempotent so that if a free runner crashes or restarts, it can pick up where it left off without wasting compute time.

## 🤝 Interaction Style

* Be concise, clear, and focused entirely on the code.
* Always assume a supportive and educational tone.
* Provide thorough documentation for any scripts generated, especially those related to data processing.
