# Publish to GitHub

Start with a private repository. The bundled vendor/fde_oci_agent wheel has no declared license; resolve redistribution rights before making the repository public. No open-source license has been selected for this project.

Install Git and GitHub CLI, then authenticate locally:

    gh auth login --hostname github.com --git-protocol https --web
    gh auth setup-git

From this extracted folder:

    git init -b main
    git add .
    git diff --cached --stat
    git diff --cached --name-only

Review the staged files. Local agent.toml, credentials, private keys, environments and task history must stay excluded. Configure git user.name and user.email if Git requests them. Then:

    git commit -m "Initial Cloud Assistant Agent v0.3.2"
    gh repo create cloud-assistant-agent --private --source=. --remote=origin --push
    gh repo view --web

Future changes:

    git add .
    git commit -m "Describe the change"
    git push

Publishing source does not expose or deploy the running VM service. No cloud credentials are needed in GitHub Actions for the included mocked test suite.

References:
- https://cli.github.com/manual/gh_auth_login
- https://cli.github.com/manual/gh_repo_create
