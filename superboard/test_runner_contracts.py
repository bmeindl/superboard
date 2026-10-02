"""Runner facts must survive fresh, resumed and compacted prompt assembly."""
from pathlib import Path

import pytest

import contract
import gc_runner


@pytest.mark.parametrize('runner,model,name,trailer,route', [
    ('claude', 'opus', 'Claude Code', 'Co-Authored-By: Claude <noreply@anthropic.com>', 'Agent/Task'),
    ('codex', 'gpt-6-astra', 'Codex', 'Co-Authored-By: Codex <noreply@openai.com>', 'spawn_agent'),
])
@pytest.mark.parametrize('mode', ['fresh', 'resume', 'compacted'])
def test_runner_facts_in_actual_prompt(monkeypatch, runner, model, name, trailer, route, mode):
    # Exclude inherited kernel/history: these may intentionally contain Claude examples.
    monkeypatch.setattr(gc_runner, '_kernel_block', lambda: '')
    monkeypatch.setattr(gc_runner, '_git_context', lambda *a, **kw: '')
    pending = {'addr': {'id': 'contracttest', 'name': 'Test', 'col': 'Now'},
               'title': 'Contract probe', 'body': [], 'last_ask': 'Propose only.',
               'thread': [{'kind': 'ask', 'text': 'Propose only.'}],
               'gc_last': 'kompaktiert' if mode == 'compacted' else ''}
    prompt = gc_runner.build_prompt(pending, resume=mode != 'fresh', runner=runner, model=model)
    variant = 'reminder' if mode == 'resume' else 'full'
    expected = gc_runner._contract_for(runner, variant, model)
    assert expected in prompt
    assert f'Active runner: {name} (`{runner}`)' in expected
    assert f'Model selection: `{model}`' in expected
    assert trailer in expected and route in expected
    assert 'requested model/alias, not proof' in expected
    assert 'supersede incompatible runner-specific' in expected
    assert 'independent-review gates still apply' in expected
    assert 'report the blocker rather than silently skipping it' in expected
    assert '{{' not in expected
    if runner in ('codex', 'opencode'):
        assert 'Co-Authored-By: Claude' not in expected
        assert 'Generated with Claude Code' not in expected
        assert 'model: "sonnet"' not in expected
    if variant == 'full':
        command = {'codex': f'{gc_runner.codex_cmd()} resume',
                   'opencode': 'opencode --session'}.get(
                       runner, f'{gc_runner.PRIVATE_CMD} --resume')
        assert f'{command} <SESSION>' in expected


def test_generic_contract_keeps_runner_facts(tmp_path: Path):
    text = contract.render('reminder', tmp_path / 'absent.md', runner='codex')
    assert 'Active runner: Codex' in text
    assert 'not supplied; consult runtime metadata' in text
    assert 'spawn_agent' in text and 'report the blocker' in text
    assert 'model: "sonnet"' not in text


def test_unknown_runner_and_variant_fail_explicitly():
    with pytest.raises(ValueError, match='Unknown contract runner'):
        contract.render('full', runner='invented')
    with pytest.raises(ValueError, match='Unknown contract variant'):
        gc_runner._contract_for('codex', 'invented')


def test_unrelated_template_examples_are_literal(tmp_path: Path):
    instance = tmp_path / 'custom.md'
    instance.write_text('<!-- contract:full.operator -->\nUse {{example}} literally. {{delegation_route}}\n<!-- /contract -->\n')
    text = contract.render('full', instance, runner='opencode')
    assert '{{example}}' in text and '{{delegation_route}}' not in text
    assert "OpenCode's task tool" in text


