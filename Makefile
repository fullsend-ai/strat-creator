.PHONY: install lint test test-unit test-integration test-e2e test-mermaid clean

install:            ## Install all dependencies via uv
	uv sync

lint:               ## Lint Python files with ruff; validate types/ against its schema (gate 1)
	uv run ruff check scripts/ tests/
	uv run python scripts/validate_types.py

test:               ## Run all tests
	uv run pytest tests/ -v --tb=short

test-unit:          ## Run unit tests only
	uv run pytest tests/test_artifact_utils.py tests/test_state.py tests/test_frontmatter_cli.py \
	       tests/test_apply_scores.py tests/test_markdown_adf.py tests/test_push_strategy.py \
	       tests/test_jql_builder.py tests/test_list_rfe_ids.py \
	       tests/test_dashboard_metrics.py \
	       tests/test_render_html.py \
	       tests/test_eval_checks.py \
	       tests/test_skill_integrity.py \
	       tests/test_type_registry.py tests/test_type_registry_pins.py \
	       tests/assess_strat -v --tb=short

test-integration:   ## Run integration tests (jira-emulator)
	uv run pytest tests/test_clone_issue.py tests/test_push_strategy_integration.py \
	       tests/test_push_refined_strategies.py \
	       tests/test_fetch_issue.py tests/test_find_strat_for_rfe.py \
	       tests/test_search_and_filter.py tests/test_pull_strategy.py \
	       tests/test_skill_paths.py -v --tb=short

test-e2e:           ## Run E2E pipeline replay tests
	uv run pytest tests/test_pipeline_e2e.py -v --tb=short

test-mermaid:       ## Run mermaid workflow validation
	uv run pytest tests/test_mermaid_workflow.py -v --tb=short

clean:              ## Remove build artifacts
	find . -type d -name __pycache__ -exec rm -rf {} + 2>/dev/null || true
	find . -type d -name .pytest_cache -exec rm -rf {} + 2>/dev/null || true
	rm -rf tmp/
