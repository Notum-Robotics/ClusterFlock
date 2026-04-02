# Contributing to ClusterFlock

Thank you for your interest in contributing to ClusterFlock!

## Getting Started

1. Fork the repository
2. Clone your fork: `git clone --recurse-submodules https://github.com/<you>/ClusterFlock.git`
3. Create a feature branch: `git checkout -b my-feature`
4. Make your changes
5. Test locally (see below)
6. Commit and push: `git push origin my-feature`
7. Open a Pull Request

## Development Setup

**nCore** (orchestrator) requires only Python 3.10+ with no third-party dependencies.

**Agents** require:
```bash
pip install huggingface_hub
```

Agent-specific build steps (llama.cpp) are handled by each agent's `setup.py`.

## Testing

- Run nCore: `python3 nCore/run.py --host 0.0.0.0 --port 1903`
- Run an agent: `python3 agents/agent_mac/run.py setup` (first time), then `python3 agents/agent_mac/run.py run`

## Code Style

- Clean, lean, simple, performant and readable code
- No unnecessary abstractions or over-engineering
- Python stdlib preferred — avoid adding third-party dependencies to nCore
- Each agent is self-contained in its directory

## Reporting Issues

Open an issue with:
- What you expected to happen
- What actually happened
- Steps to reproduce
- Agent type and hardware (if relevant)

## License

By contributing, you agree that your contributions will be licensed under the MIT License.
