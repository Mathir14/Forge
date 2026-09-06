# Forge Roadmap

## v0.1.0 (Current)
- [x] Core pipeline: Architect -> Planner -> Executor -> Reviewer -> Critic
- [x] OpenCode and Antigravity adapters
- [x] Run management and artifact storage
- [x] Machine report protocol
- [x] Interactive (`forge run`) and autonomous (`forge auto`) modes
- [x] Critic-to-pipeline linking (`--from-critic`)

## v0.2.0 (Planned)
- [ ] Structured logging across all modules
- [ ] Additional adapters (Aider, Continue, Cursor)
- [ ] Web UI for run visualization
- [ ] Parallel stage execution
- [ ] Cost tracking per run
- [ ] Custom role templates

## v0.3.0 (Future)
- [ ] Plugin system for custom stages
- [ ] Distributed execution
- [ ] CI/CD integration
- [ ] Run comparison and diffing
- [ ] Performance benchmarks

## Known Issues
- Empty project docs reduce agent context quality
- Git porcelain parsing doesn't handle all edge cases
- Token estimation is crude (len//4)
