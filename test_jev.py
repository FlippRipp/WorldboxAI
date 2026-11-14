import asyncio
import json
from pathlib import Path
from types import SimpleNamespace

import httpx
import pytest

from backend.engine.jev import JevService, choice, noul, Uncertain, fallback_generate
from backend.engine import jev_tasks
from backend.engine.llm_inspector import LLMInspector
from backend.engine.llm_call_log import LLMCallLog
from test_rpg_xp_gain import _load_backend as load_rpg, _char
from test_npc_system import _load_backend as load_npc, _state as npc_state


def response_for(request, values=None):
    values = values or {}
    answers = {}
    for key, question in request['questions'].items():
        if question['type'] == 'noul':
            answers[key] = {'type': 'noul', 'noul': values.get(key, 0.0)}
        else:
            options = question['criteria']
            selected = values.get(key, 'none' if 'none' in options else next(iter(options)))
            answers[key] = {'type': 'choice', 'choice': selected, 'confidence': 1.0,
                            'probabilities': {k: float(k == selected) for k in options}}
    return {'model': 'jev-test-version', 'answers': answers,
            'usage': {'input_tokens': 5000, 'output_tokens': 120}}


def setup_jev(tmp_path, values=None, handler=None, reply='{}'):
    requests, generated = [], []
    def respond(request):
        payload = json.loads(request.content)
        requests.append(payload)
        if handler:
            return handler(request, payload)
        selected = values(payload) if callable(values) else values
        return httpx.Response(200, json=response_for(payload, selected))
    service = JevService(tmp_path / 'jev' / 'config.json', transport=httpx.MockTransport(respond))
    service.save({'enabled': True, 'api_key': 'test-secret'})
    inspector = LLMInspector()
    inspector.set_call_logger(LLMCallLog(str(tmp_path / 'logs')))
    async def decide(state, questions, *, module, step):
        return await service.decide(state, questions, module=module, step=step, inspector=inspector)
    async def generate(prompt, model_preference='balanced'):
        generated.append((prompt, model_preference))
        return reply(prompt) if callable(reply) else reply
    sdk = SimpleNamespace(llm=SimpleNamespace(decide=decide, generate=generate))
    return service, sdk, requests, generated, inspector


def run(coro):
    return asyncio.run(coro)


def test_configuration_is_separate_masked_and_persistent(tmp_path):
    path = tmp_path / 'jev' / 'config.json'
    service = JevService(path)
    assert service.public_config()['enabled'] is False
    assert not path.exists()
    service.save({'api_key': 'secret', 'rpg_enabled': False})
    service.save({'enabled': True})
    reloaded = JevService(path)
    assert reloaded.public_config() == {'enabled': True, 'rpg_enabled': False,
        'npc_enabled': True, 'plot_enabled': True, 'tts_enabled': True,
        'model': 'jev-latest', 'api_key_set': True}
    assert 'secret' not in json.dumps(reloaded.public_config())
    reloaded.save({'remove_api_key': True})
    assert not reloaded.public_config()['api_key_set']
    with pytest.raises(ValueError):
        reloaded.save({'enabled': 'yes'})


@pytest.mark.parametrize('mode,updates,mod', [
    ('mock', {}, jev_tasks.RPG),
    ('live', {'enabled': False}, jev_tasks.RPG),
    ('live', {'rpg_enabled': False}, jev_tasks.RPG),
    ('live', {'tts_enabled': False}, 'wb_tts'),
    ('live', {'remove_api_key': True}, jev_tasks.RPG),
])
def test_disabled_missing_and_mock_never_call_network(tmp_path, mode, updates, mod):
    service, _, requests, _, _ = setup_jev(tmp_path)
    service.save(updates)
    result = run(service.decide('story', {'ok': noul('yes?')}, module=mod, step='test', mode=mode))
    assert not result.answers and not requests
    with pytest.raises(Uncertain):
        result.yes('ok')


@pytest.mark.parametrize('status', [401, 422, 429, 500, 529])
def test_http_errors_are_redacted_no_retry(tmp_path, status):
    service, sdk, requests, generated, inspector = setup_jev(tmp_path,
        handler=lambda *_: httpx.Response(status, text='test-secret should never be logged'))
    async def task():
        d = await sdk.llm.decide('x', {'ok': noul('yes?')}, module=jev_tasks.RPG, step='test')
        await fallback_generate(sdk, d, 'original prompt', 'fastest')
    run(task())
    assert len(requests) == len(generated) == 1
    call = inspector.get_calls()[0]
    assert call['decision_outcome'] == 'fallback' and call['fallback_reason'] == f'HTTP {status}'
    assert call['estimated_cost_usd'] is None
    assert 'test-secret' not in json.dumps(call)
    assert 'test-secret' not in (tmp_path / 'logs' / 'llm_calls.jsonl').read_text()


@pytest.mark.parametrize('corrupt', [
    lambda p: p['answers'].clear(),
    lambda p: p['answers']['pick'].update(choice='invented'),
    lambda p: p['answers']['pick'].update(confidence=-1),
    lambda p: p['answers']['pick'].update(probabilities={'a': 0.2, 'b': 0.2}),
    lambda p: p['answers']['pick'].update(type='noul'),
    lambda p: p.update(usage={'input_tokens': -1, 'output_tokens': 0}),
])
def test_malformed_answers_fall_back(tmp_path, corrupt):
    def handler(request, payload):
        result = response_for(payload)
        corrupt(result)
        return httpx.Response(200, json=result)
    service, _, _, _, _ = setup_jev(tmp_path, handler=handler)
    d = run(service.decide('x', {'pick': choice('pick', {'a': 'A', 'b': 'B'})}, module=jev_tasks.RPG, step='test'))
    assert not d.answers and d.outcome == 'fallback'


def test_deadline_and_cancellation(tmp_path):
    async def slow(request):
        await asyncio.sleep(5)
    service = JevService(tmp_path / 'config.json', transport=httpx.MockTransport(slow))
    service.save({'enabled': True, 'api_key': 'test'})
    service.timeout = 0.02
    inspector = LLMInspector()
    d = run(service.decide('x', {'ok': noul('yes?')}, module=jev_tasks.RPG, step='timeout', inspector=inspector))
    assert d.outcome == 'fallback' and d.reason == 'TimeoutError'
    async def cancel():
        service.timeout = 3
        task = asyncio.create_task(service.decide('x', {'ok': noul('yes?')}, module=jev_tasks.RPG, step='cancel', inspector=inspector))
        await asyncio.sleep(0.01)
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task
    run(cancel())
    assert inspector.get_calls()[0]['status'] == 'cancelled'


def test_option_limit_falls_back_without_truncating(tmp_path):
    service, _, requests, _, _ = setup_jev(tmp_path)
    d = run(service.decide('all context', {'pick': choice('pick', {str(i): str(i) for i in range(256)})},
                           module=jev_tasks.RPG, step='test'))
    assert not requests and d.reason == 'unsupported option count'


def test_usage_version_and_persistent_outcome(tmp_path):
    _, sdk, _, _, inspector = setup_jev(tmp_path, {'ok': 1})
    async def task():
        d = await sdk.llm.decide('x', {'ok': noul('yes?')}, module=jev_tasks.RPG, step='test')
        assert d.yes('ok')
        await d.finish('accepted')
    run(task())
    record = inspector.get_calls()[0]
    assert record['model'] == 'jev-test-version'
    assert record['estimated_cost_usd'] == pytest.approx(0.00021)
    assert record['tokens_out'] == 120 and record['decision_outcome'] == 'accepted'
    assert json.loads((tmp_path / 'logs' / 'llm_calls.jsonl').read_text()) == record


def rpg_state(rpg, **extra):
    return {'turn': 3, 'history': ['The guard hears the persuasive argument and opens the gate.'],
            'last_input_text': 'I persuade the guard.', 'input_text': 'I persuade the guard.',
            'module_configs': {'wb_core_rpg': {}}, 'module_data': {'wb_core_rpg': rpg}, **extra}


def npc_module_state(state):
    from backend.engine.graph import EngineGraph
    path = Path(__file__).parent / 'modules' / 'wb_npc_system' / 'manifest.json'
    manifest = json.loads(path.read_text(encoding='utf-8'))
    return EngineGraph._build_module_state(None, state, 'wb_npc_system', manifest['consumes'])


def test_action_practice_passives_curses_and_exact_skill(tmp_path):
    backend = load_rpg()
    skills = {'fire': {'rating': 3, 'type': 'active'}, 'fire dance': {'rating': 4, 'type': 'active'},
              'sight': {'rating': 5, 'type': 'passive', 'description': 'See in darkness.'},
              'hex': {'rating': 2, 'type': 'curse', 'description': 'Dancing hurts.'}}
    _, sdk, requests, generated, _ = setup_jev(tmp_path, {
        'substantive': 1, 'feasibility': '8', 'difficulty': 'moderate',
        'skill': 'option_1', 'practice': 'option_1', 'curse': 'option_0', 'passive_0': 1})
    state = rpg_state(_char(skills=skills))
    state['module_configs']['wb_core_rpg']['progression_system'] = 'practice'
    result = run(backend.on_gather_context(state, sdk))['module_data']['wb_core_rpg']
    assert not generated and len(requests) == 1
    assert result['practice_counters'] == {'fire dance': 4}
    assert result['action_assessment']['curse_triggered'] == 'hex'
    assert 'See in darkness.' in result['action_assessment']['passive_effects']


@pytest.mark.parametrize('substantive,expected_calls', [(0, 0), (0.5, 1)])
def test_trivial_or_uncertain_action(tmp_path, substantive, expected_calls):
    backend = load_rpg()
    _, sdk, _, generated, _ = setup_jev(tmp_path, {'substantive': substantive}, reply='{"skip":true}')
    char = backend.Character.from_dict(_char())
    assert run(backend._assess_action('Hello!', char, {}, sdk)) == {}
    assert len(generated) == expected_calls


def test_failure_reason_uses_existing_fact_and_custom_directive(tmp_path):
    backend = load_rpg()
    def values(request):
        if 'evidence' in request['questions']:
            options = request['questions']['evidence']['criteria']
            evidence = next(k for k, v in options.items() if isinstance(v, dict) and v['text'] == 'Gravity cannot be reversed.')
            return {'category': 'world_rule', 'evidence': evidence}
        return {'substantive': 1, 'feasibility': '1', 'difficulty': 'impossible'}
    _, sdk, requests, generated, _ = setup_jev(tmp_path, values)
    result = run(backend._assess_action('Reverse gravity', backend.Character.from_dict(_char()), {}, sdk,
        world_data={'lore': {'premise': 'Gravity cannot be reversed.'}},
        instructions={'action_assessment': 'Respect immutable physics.'}))
    assert 'Gravity cannot be reversed.' in result['failure_reason']
    assert 'Respect immutable physics.' in requests[0]['state']['task']
    assert len(requests) == 2 and not generated


@pytest.mark.parametrize('source,labels,value', [
    ('context.character.stats.power', ('power',), '1'),
    ('context.character.hp', ('hp',), '2'),
    ('context.character.skills.lifting.rating', ('lifting', 'rating'), '3'),
])
def test_scalar_failure_evidence_keeps_identity_in_narrator_ruling(tmp_path, source, labels, value):
    backend = load_rpg()
    def values(request):
        if 'evidence' in request['questions']:
            options = request['questions']['evidence']['criteria']
            evidence = next(k for k, v in options.items()
                            if isinstance(v, dict) and v['source'] == source)
            assert options[evidence]['text'] == value
            return {'category': 'capability', 'evidence': evidence}
        return {'substantive': 1, 'feasibility': '1', 'difficulty': 'extreme'}
    _, sdk, requests, generated, _ = setup_jev(tmp_path, values)
    char = backend.Character.from_dict(_char(stats={'power': 1}, hp=2,
        skills={'lifting': {'rating': 3, 'type': 'active'}}))
    action = 'Lift the heavy portcullis.'
    char.action_assessment = run(backend._assess_action(action, char, {}, sdk))

    reason = char.action_assessment['failure_reason']
    assert all(label in reason.lower() for label in labels)
    assert value in reason
    narrator_prompt = backend._build_action_feasibility_prompt(char, action, {})
    assert reason in narrator_prompt
    assert len(requests) == 2 and not generated


@pytest.mark.parametrize('amount,reason,override,fallback,expected', [
    ('10', 'success', None, False, 10), ('0', 'routine', None, False, 0),
    ('outside', 'extraordinary', None, True, 70), ('0', 'success', None, True, 70),
    ('10', 'success', 'Award 70 XP per challenge.', True, 70)])
def test_xp_default_custom_and_single_award(tmp_path, amount, reason, override, fallback, expected):
    backend = load_rpg()
    _, sdk, requests, generated, _ = setup_jev(tmp_path, {'amount': amount, 'reason': reason},
                                             reply='{"xp_awarded":70,"reason":"custom"}')
    char = backend.Character.from_dict(_char(action_assessment={'feasibility': 8, 'difficulty': 'moderate'}))
    state = rpg_state(char.to_dict())
    if override:
        state['module_instructions'] = {'xp_judgment': override}
    run(backend._judge_xp(char, state, {}, sdk))
    assert char.xp == expected
    assert len(generated) == int(fallback)
    assert len(requests) == (0 if override else 1)


@pytest.mark.parametrize('probability,expected_calls', [(0.01, 0), (0.03, 1), (0.5, 1), (1, 1)])
def test_external_event_screen_preserves_full_scene(tmp_path, probability, expected_calls):
    backend = load_rpg()
    _, sdk, requests, generated, _ = setup_jev(tmp_path, {'events': probability},
                                             reply='{"added":[],"removed":[],"altered":[]}')
    state = rpg_state(_char(), history=['START ' + 'narrative ' * 1200 + ' END'])
    assert not run(backend._detect_external_skill_events(backend.Character.from_dict(_char()), state, {}, sdk))
    assert len(generated) == expected_calls
    assert state['history'][0] in requests[0]['state']['task']
    if generated:
        assert state['history'][0] in generated[0][0]


@pytest.mark.parametrize('probabilities,expected,fallback', [
    ({'npc_0': 1, 'npc_1': 0}, {'n1'}, False),
    ({'npc_0': 0, 'npc_1': 0}, set(), False),
    ({'npc_0': 1, 'npc_1': 0.5}, {'n2'}, True)])
def test_npc_presence_atomic_and_fallback(tmp_path, probabilities, expected, fallback):
    backend = load_npc()
    _, sdk, _, generated, _ = setup_jev(tmp_path, probabilities, reply='["n2"]')
    candidates = [{'id': 'n1', 'name': 'A'}, {'id': 'n2', 'name': 'B'}]
    result = run(backend._llm_scene_presence(npc_state(), sdk, candidates))
    assert result == expected and len(generated) == int(fallback)


@pytest.mark.parametrize('phase', ['presence', 'introduction'])
@pytest.mark.parametrize('current_action', ['I seek a smith to repair my sword.', ''])
@pytest.mark.parametrize('project_state', [False, True])
def test_npc_preturn_decisions_use_current_action(tmp_path, phase, current_action, project_state):
    from test_npc_system import _unmet_npc
    backend = load_npc()
    _, sdk, requests, generated, _ = setup_jev(tmp_path)
    candidate = _unmet_npc('n1', 'Borin')
    state = npc_state({'n1': candidate})
    state['last_input_text'] = 'I leave the smithy and hide alone in the forest.'
    state['input_text'] = current_action
    if project_state:
        state = npc_module_state(state)

    if phase == 'presence':
        assert run(backend._llm_scene_presence(state, sdk, [candidate])) == set()
    else:
        assert run(backend._introduction_pass(state, sdk)) is None

    assert len(requests) == 1 and not generated
    assert requests[0]['state']['player_action'] == current_action


@pytest.mark.parametrize('phase', ['motivation', 'records', 'creation'])
@pytest.mark.parametrize('completed_action', ['I ask Borin to bring me the antidote.', ''])
def test_npc_postturn_decisions_use_completed_action(tmp_path, phase, completed_action):
    from test_npc_system import _present_npc
    backend = load_npc()
    _, sdk, requests, _, _ = setup_jev(tmp_path)
    candidate = _present_npc('n1', 'Borin')
    bank = {} if phase == 'creation' else {'n1': candidate}
    state = npc_state(bank, mutation_config={'generator_frequency': 1},
                      history=['Borin waits beside the player at the tavern.'])
    state['last_input_text'] = completed_action
    state['input_text'] = 'I hide from Borin in the forest.'
    state = npc_module_state(state)
    bank = state['module_data']['wb_npc_system']['characters']

    if phase == 'motivation':
        assert run(backend._llm_motivated_ids(state, sdk, [candidate])) == set()
    elif phase == 'records':
        assert not run(backend._track_character_changes(state, bank, sdk))
    else:
        run(backend.on_librarian(state, sdk))

    assert len(requests) == 1
    assert requests[0]['state']['player_action'] == completed_action


def test_npc_motivation_and_record_screen(tmp_path):
    backend = load_npc()
    _, sdk, _, generated, _ = setup_jev(tmp_path, {'npc_0': 0})
    candidate = {'id': 'n1', 'name': 'A', 'introduced': True, 'status': 'active',
                 'location_node_id': 'node_market', 'location_region': 'Harborside'}
    state = npc_state({'n1': candidate})
    assert run(backend._llm_motivated_ids(state, sdk, [candidate])) == set()
    assert not run(backend._track_character_changes(state, state['module_data']['wb_npc_system']['characters'], sdk))
    assert not generated


def test_provider_switch_preserves_jev_config(tmp_path):
    from backend.engine.provider_manager import ProviderManager
    pm = ProviderManager(str(tmp_path / 'providers'))
    pm.decisions.save({'enabled': True, 'api_key': 'separate'})
    before = pm.decisions.public_config()
    pm.set_active('openrouter')
    pm.set_active('gemini')
    assert pm.decisions.public_config() == before
    assert 'jev' not in [p['id'] for p in pm.get_all()]


def test_api_key_masking_remove_and_turn_guard(tmp_path, monkeypatch):
    from fastapi.testclient import TestClient
    from backend.api import server
    service = JevService(tmp_path / 'config.json')
    monkeypatch.setattr(server.engine.llm, 'decisions', service)
    client = TestClient(server.app)
    monkeypatch.setattr(server.chat_hub, 'turn_running', lambda: False)
    saved = client.put('/api/jev/config', json={'api_key': 'never-return-me', 'enabled': True})
    assert saved.status_code == 200 and saved.json()['api_key_set']
    assert 'never-return-me' not in saved.text
    assert client.put('/api/jev/config', json={'npc_enabled': False}).json()['api_key_set']
    assert 'never-return-me' not in client.get('/api/jev/config').text
    monkeypatch.setattr(server.chat_hub, 'turn_running', lambda: True)
    assert client.put('/api/jev/config', json={'enabled': False}).status_code == 409
    monkeypatch.setattr(server.chat_hub, 'turn_running', lambda: False)
    assert not client.put('/api/jev/config', json={'remove_api_key': True}).json()['api_key_set']


def test_choice_uncertainty_nonfinite_and_inconsistent_ruling(tmp_path):
    def handler(request, payload):
        data = response_for(payload, {'substantive': 1, 'feasibility': '9', 'difficulty': 'impossible'})
        if payload['state'] == 'nonfinite':
            data['answers']['ok']['noul'] = float('nan')
        return httpx.Response(200, content=json.dumps(data))
    service, sdk, _, generated, _ = setup_jev(tmp_path, handler=handler, reply='{"skip":true}')
    d = run(service.decide('nonfinite', {'ok': noul('yes?')}, module=jev_tasks.RPG, step='test'))
    assert not d.answers
    backend = load_rpg()
    assert run(backend._assess_action('Fly', backend.Character.from_dict(_char()), {}, sdk)) == {}
    assert len(generated) == 1
    from backend.engine.jev import Decision
    d = Decision(answers={'pick': {'type': 'choice', 'choice': 'a', 'confidence': 0.89}})
    with pytest.raises(Uncertain):
        d.pick('pick')


def test_failed_grounding_falls_back_before_practice_update(tmp_path):
    backend = load_rpg()
    values = {'substantive': 1, 'feasibility': '1', 'difficulty': 'impossible',
              'practice': 'option_0', 'category': 'unsupported', 'evidence': 'unsupported'}
    _, sdk, _, generated, inspector = setup_jev(tmp_path, values,
        reply=lambda prompt: '{"skip":true}' if 'Assess this RPG' in prompt else 'none')
    state = rpg_state(_char(skills={'fire': {'rating': 3, 'type': 'active'}}))
    state['module_configs']['wb_core_rpg']['progression_system'] = 'practice'
    result = run(backend.on_gather_context(state, sdk))['module_data']['wb_core_rpg']
    assert result['practice_counters'] == {} and len(generated) == 2
    assert all(c['decision_outcome'] == 'fallback' for c in inspector.get_calls())


def test_introduction_grounding_and_turn_zero(tmp_path):
    backend = load_npc()
    candidate = {'id': 'n1', 'name': 'Borin', 'pitch': 'A smith who supplies the guard.', 'role': 'ally'}
    _, sdk, requests, generated, _ = setup_jev(tmp_path,
        lambda p: {'npc': 'option_0'} if 'npc' in p['questions'] else {'category': 'need', 'evidence': 'option_0'})
    value, d = run(jev_tasks.introduce(sdk, npc_state(), [candidate], 'The player needs a smith.'))
    assert value['npc_id'] == 'n1' and 'established context' in value['reason']
    assert d.outcome == 'accepted' and len(requests) == 2 and not generated
    state = npc_state({'n1': candidate})
    state['turn'] = 0
    assert run(backend._introduction_pass(state, sdk)) is None
    assert len(requests) == 2


def test_record_screen_does_not_hide_rename(tmp_path):
    from test_npc_system import _present_npc, _make_sdk
    backend = load_npc()
    _, sdk, requests, generated, _ = setup_jev(tmp_path, {'npc_0': 0.5},
        reply='{"updates":[{"npc_id":"n1","name":"Veyra","change_note":"Revealed name"}]}')
    sdk.memory = _make_sdk()[0].memory
    npc = _present_npc('n1', 'The Hooded Stranger')
    state = npc_state({'n1': npc}, history=['The hooded stranger reveals she is Veyra.'])
    bank = backend._get_bank(state)
    assert run(backend._track_character_changes(state, bank, sdk))
    assert bank['n1']['name'] == 'Veyra' and len(generated) == 1
    assert 'revealed or adopted names' in requests[0]['questions']['npc_0']['instructions']


@pytest.mark.parametrize('demand,probability,generated_count', [(True, 0.01, 0), (True, 0.5, 1), (True, 1, 1), (False, 0.01, 1)])
def test_creation_screen_and_pool_filling(tmp_path, demand, probability, generated_count):
    backend = load_npc()
    _, sdk, requests, generated, _ = setup_jev(tmp_path, {'new_character': probability}, reply='{"npcs":[]}')
    state = npc_state(mutation_config={'generator_frequency': 1, 'demand_driven_generation': demand})
    run(backend.on_librarian(state, sdk))
    generation = [p for p, _ in generated if 'casting director' in p or 'Create 3 new NPC' in p]
    assert len(generation) == generated_count
    assert any('new_character' in p['questions'] for p in requests) == demand


def test_manual_npc_generation_never_screened(tmp_path):
    backend = load_npc()
    _, sdk, requests, generated, _ = setup_jev(tmp_path, reply='{"npc":null}')
    run(backend._generate_random_character(npc_state(), sdk))
    assert not requests and len(generated) == 1


def test_concurrent_fallbacks_keep_explicit_attribution(tmp_path, monkeypatch):
    from backend.engine.llm import LLMService
    from backend.sdk.llm_bridge import LLMBridge
    import backend.engine.llm as llm_module
    from backend.engine.jev import Decision
    inspector = LLMInspector()
    llm = LLMService(mode='live')
    llm.inspector = inspector
    monkeypatch.setattr(llm, '_reasoning_kwargs', lambda _: {})
    async def completion(**kwargs):
        await asyncio.sleep(0)
        return SimpleNamespace(choices=[SimpleNamespace(message=SimpleNamespace(content='{}'), finish_reason='stop')],
                               usage=SimpleNamespace(to_dict=lambda: {'prompt_tokens': 10, 'completion_tokens': 2}))
    monkeypatch.setattr(llm_module, 'acompletion', completion)
    bridge = LLMBridge()
    bridge._set_service(llm)
    sdk = SimpleNamespace(llm=bridge)
    async def task():
        await asyncio.gather(*(fallback_generate(sdk, Decision(call_id=mid, module=mid, step=mid), mid, 'fastest')
                               for mid in ('wb_core_rpg', 'wb_npc_system')))
    run(task())
    records = inspector.get_calls()
    assert len(records) == 2
    assert {r['module_source'] for r in records} == {'wb_core_rpg', 'wb_npc_system'}
    assert all(r['module_source'] == r['decision_parent_id'] and r['step'] == r['module_source'] + ':fallback' for r in records)


def test_decision_receives_original_state_and_questions(tmp_path):
    service, _, requests, _, _ = setup_jev(tmp_path, {'ok': 1})
    questions = {'ok': noul('yes?')}
    original = {'history': 'Original adventure facts', 'hp': 8}
    decision = run(service.decide(original, questions, module=jev_tasks.RPG, step='test'))
    assert decision.answers
    assert len(requests) == 1
    assert requests[0]['state'] == original
    assert requests[0]['questions'] == questions


def test_evaluation_cases_have_valid_expected_answers():
    from tools.evaluate_jev import CASES, matches
    assert len(CASES) >= 10 and len({c['id'] for c in CASES}) == len(CASES)
    for case in CASES:
        assert set(case['questions']) == set(case['expected'])
        actual = {}
        for key, expected in case['expected'].items():
            actual[key] = expected[0] if isinstance(expected, list) else expected
            question = case['questions'][key]
            if question['type'] == 'choice':
                assert actual[key] in question['criteria']
            else:
                assert isinstance(actual[key], bool)
        assert matches(actual, case['expected'])


def test_openrouter_active_provider_routes_jev_until_typesafe_key_overrides(tmp_path):
    from backend.engine.provider_manager import ProviderManager
    from backend.engine.jev import ENDPOINT, OPENROUTER_ENDPOINT
    seen = []
    def respond(request):
        payload = json.loads(request.content)
        seen.append((str(request.url), request.headers.get('Authorization'), payload['model']))
        data = response_for(payload, {'ok': 1})
        data['model'] = 'typesafe/jev-1.14-20261014'
        data['usage']['cost'] = 0.00001365
        return httpx.Response(200, json=data)

    pm = ProviderManager(str(tmp_path / 'providers'))
    pm.decisions.transport = httpx.MockTransport(respond)
    pm.decisions.save({'enabled': True, 'model': 'typesafe/jev-latest'})
    pm.save_config('openrouter', {'api_key': 'or-secret-key'})

    # When OpenRouter is not the active provider and no TypeSafe key is saved, no call is made.
    pm.set_active('gemini')
    d_inactive = run(pm.decisions.decide('x', {'ok': noul('yes?')}, module=jev_tasks.RPG, step='test'))
    assert not d_inactive.answers and d_inactive.reason == 'missing API key' and not seen

    # When OpenRouter is the active provider, Jev routes to OpenRouter's SystemOne endpoint.
    pm.set_active('openrouter')
    assert pm.decisions.has_credentials()
    inspector = LLMInspector()
    d_or = run(pm.decisions.decide('x', {'ok': noul('yes?')}, module=jev_tasks.RPG, step='test', inspector=inspector))
    assert d_or.yes('ok')
    run(d_or.finish('accepted'))
    assert seen[-1] == (OPENROUTER_ENDPOINT, 'Bearer or-secret-key', 'jev-latest')
    assert inspector.get_calls()[0]['estimated_cost_usd'] == pytest.approx(0.00001365)

    # A saved TypeSafe API key takes precedence and routes directly to TypeSafe.
    pm.decisions.save({'api_key': 'ts-direct-key', 'model': '~typesafe/jev-latest'})
    d_ts = run(pm.decisions.decide('x', {'ok': noul('yes?')}, module=jev_tasks.RPG, step='test'))
    assert d_ts.yes('ok')
    assert seen[-1] == (ENDPOINT, 'Bearer ts-direct-key', 'jev-latest')


def test_nanogpt_active_provider_routes_jev_until_typesafe_key_overrides(tmp_path):
    from backend.engine.provider_manager import ProviderManager
    from backend.engine.jev import ENDPOINT, NANOGPT_ENDPOINT
    seen = []
    def respond(request):
        payload = json.loads(request.content)
        seen.append((str(request.url), request.headers.get('Authorization'), payload['model']))
        data = response_for(payload, {'ok': 1})
        data['model'] = 'typesafe/jev-1.14-20261014'
        data['usage']['cost'] = 0.00001365
        return httpx.Response(200, json=data)

    pm = ProviderManager(str(tmp_path / 'providers'))
    pm.decisions.transport = httpx.MockTransport(respond)
    pm.decisions.save({'enabled': True, 'model': 'nano-gpt/typesafe/jev-latest'})
    pm.save_config('nanogpt', {'api_key': 'ng-secret-key'})

    pm.set_active('nanogpt')
    assert pm.decisions.has_credentials()
    d_ng = run(pm.decisions.decide('x', {'ok': noul('yes?')}, module=jev_tasks.RPG, step='test'))
    assert d_ng.yes('ok')
    assert seen[-1] == (NANOGPT_ENDPOINT, 'Bearer ng-secret-key', 'jev-latest')

    pm.decisions.save({'api_key': 'ts-direct-key', 'model': '~typesafe/jev-latest'})
    d_ts = run(pm.decisions.decide('x', {'ok': noul('yes?')}, module=jev_tasks.RPG, step='test'))
    assert d_ts.yes('ok')
    assert seen[-1] == (ENDPOINT, 'Bearer ts-direct-key', 'jev-latest')


def test_realistic_multi_bucket_distributions_succeed_on_rpg_and_npc_tasks(tmp_path):
    backend = load_rpg()
    def handler(request, payload):
        qs = payload['questions']
        if 'feasibility' in qs:
            # Character has no curses; 'curse' should not even be asked.
            assert 'curse' not in qs
            return httpx.Response(200, json={
                'model': 'typesafe/jev-1.14-20261014',
                'usage': {'input_tokens': 350, 'output_tokens': 40, 'cost': 0.0000147},
                'answers': {
                    'substantive': {'type': 'noul', 'noul': 0.94},
                    'feasibility': {
                        'type': 'choice', 'choice': '7', 'confidence': 0.29,
                        'probabilities': {'1': 0.0, '2': 0.0, '3': 0.0, '4': 0.02, '5': 0.14,
                                          '6': 0.33, '7': 0.36, '8': 0.15, '9': 0.0, '10': 0.0},
                    },
                    'difficulty': {
                        'type': 'choice', 'choice': 'moderate', 'confidence': 0.68,
                        'probabilities': {'trivial': 0.02, 'easy': 0.25, 'moderate': 0.73,
                                          'hard': 0.0, 'extreme': 0.0, 'impossible': 0.0},
                    },
                    'skill': {
                        'type': 'choice', 'choice': 'option_0', 'confidence': 1.0,
                        'probabilities': {'option_0': 1.0, 'none': 0.0, 'unsupported': 0.0},
                    },
                    'passive_0': {'type': 'noul', 'noul': 0.14},
                },
            })
        if 'amount' in qs:
            probs = {str(n): 0.0 for n in range(51)}
            probs.update({'8': 0.35, '10': 0.23, '5': 0.14, '12': 0.08, '4': 0.04, '15': 0.16, 'outside': 0.0})
            return httpx.Response(200, json={
                'model': 'typesafe/jev-1.14-20261014',
                'usage': {'input_tokens': 300, 'output_tokens': 30},
                'answers': {
                    'amount': {'type': 'choice', 'choice': '8', 'confidence': 0.34, 'probabilities': probs},
                    'reason': {
                        'type': 'choice', 'choice': 'success', 'confidence': 0.89,
                        'probabilities': {'success': 0.91, 'failure': 0.0, 'ingenuity': 0.07,
                                          'extraordinary': 0.0, 'routine': 0.02, 'unsupported': 0.0},
                    },
                },
            })
        return httpx.Response(200, json=response_for(payload))

    _, sdk, requests, generated, _ = setup_jev(tmp_path, handler=handler)
    char = backend.Character.from_dict(_char(skills={
        'silver tongue': {'rating': 5, 'type': 'active'},
        'keen ears': {'rating': 4, 'type': 'passive', 'description': 'Hear whispers.'},
    }))
    assessment = run(backend._assess_action('Persuade the guard', char, {}, sdk))
    assert assessment['feasibility'] == 7
    assert assessment['difficulty'] == 'moderate'
    assert assessment['skill_used'] == 'silver tongue'
    assert assessment['passive_effects'] == ''
    char.action_assessment = assessment
    assert run(backend._judge_xp(char, rpg_state(char.to_dict()), {}, sdk))
    assert char.xp == 8
    assert len(requests) == 2 and not generated


def test_plot_injection_classifier(tmp_path):
    service, sdk, requests, _, _ = setup_jev(tmp_path, {'inject': 'withhold'})
    thread = {
        'title': 'The Salt Baron',
        'mode': 'intrigue',
        'hook': 'Rumors spread across the docks.',
        'challenge': 'Enforcers corner you in the alley.',
        'stakes': 'The harbor district.',
    }
    state = {
        'turn': 4,
        'history': ['You sit quietly with Lyra by the fireplace, sharing old memories.'],
        'input_text': 'I hold her hand and tell her how much she means to me.',
    }
    mode = run(jev_tasks.plot_injection(sdk, state, thread, last_assessment=None))
    assert mode == 'withhold'
    assert len(requests) == 1
    ctx = requests[0]['state']
    assert 'last_turn_assessment' not in ctx
    assert 'determine readiness directly' in requests[0]['questions']['inject']['instructions']

    # When a fresh assessment is provided, it is included in context.
    fresh = {'turn': 4, 'scene_mode': 'hook_only', 'thread_engaged': False, 'player_interest': 'indifferent', 'momentum': 'steady'}
    mode2 = run(jev_tasks.plot_injection(sdk, state, thread, last_assessment=fresh))
    assert mode2 == 'withhold'
    assert requests[1]['state']['last_turn_assessment']['scene_mode'] == 'hook_only'

    # Disabling plot_enabled skips the JEV network call and returns None.
    service.save({'plot_enabled': False})
    mode3 = run(jev_tasks.plot_injection(sdk, state, thread, last_assessment=fresh))
    assert mode3 is None
    assert len(requests) == 2


def test_plot_injection_realistic_multi_bucket_distributions(tmp_path):
    thread = {
        'title': "The Saintly Princess's Golden Leash",
        'mode': 'intrigue',
        'hook': 'Seraphina rests breathless against your chest.',
        'challenge': 'Deciding whether to maintain the facade or act.',
        'stakes': 'Control in the royal court.',
    }
    state = {'turn': 15, 'history': ['A private moment in the tower chamber.'], 'input_text': ''}
    cases = iter([
        # Realistic bedroom withhold (conf 0.55 < 0.90, withhold 0.70 >= 0.60) -> accepted
        ('withhold', 0.55, {'withhold': 0.70, 'hook_only': 0.12, 'active': 0.18}),
        # Realistic active turn (conf 0.85 < 0.90, active 0.90, non-withhold 0.94 >= 0.85) -> accepted
        ('active', 0.85, {'withhold': 0.06, 'hook_only': 0.04, 'active': 0.90}),
        # Realistic hook_only turn (conf 0.65 < 0.90, hook_only 0.78, non-withhold 0.90 >= 0.85) -> accepted
        ('hook_only', 0.65, {'withhold': 0.10, 'hook_only': 0.78, 'active': 0.12}),
        # Ambiguous active with too much withhold mass (0.28 > 0.15) -> fallback None
        ('active', 0.46, {'withhold': 0.28, 'hook_only': 0.08, 'active': 0.64}),
        # Weak withhold below 0.60 -> fallback None
        ('withhold', 0.28, {'withhold': 0.52, 'hook_only': 0.18, 'active': 0.30}),
    ])

    def handler(request, payload):
        selected, conf, probs = next(cases)
        criteria = payload['questions']['inject']['criteria']
        assert 'takes precedence' in criteria['withhold']
        assert 'NOT in an intimate' in criteria['active']
        return httpx.Response(200, json={
            'model': 'typesafe/jev-1.14-20261014',
            'usage': {'input_tokens': 400, 'output_tokens': 41},
            'answers': {
                'inject': {
                    'type': 'choice',
                    'choice': selected,
                    'confidence': conf,
                    'probabilities': probs,
                },
            },
        })

    _, sdk, _, _, inspector = setup_jev(tmp_path, handler=handler)
    assert run(jev_tasks.plot_injection(sdk, state, thread)) == 'withhold'
    assert run(jev_tasks.plot_injection(sdk, state, thread)) == 'active'
    assert run(jev_tasks.plot_injection(sdk, state, thread)) == 'hook_only'
    assert run(jev_tasks.plot_injection(sdk, state, thread)) is None
    assert run(jev_tasks.plot_injection(sdk, state, thread)) is None
    outcomes = [c['decision_outcome'] for c in reversed(inspector.get_calls())]
    assert outcomes == ['accepted', 'accepted', 'accepted', 'fallback', 'fallback']


def test_plugin_modules_can_decide_when_jev_enabled(tmp_path):
    service, sdk, requests, _, inspector = setup_jev(tmp_path, {'active': 1.0})
    d = run(sdk.llm.decide({'prose': 'A gentle kiss.'}, {'active': noul('Is physical touch active?')},
                           module='wb_toy_link', step='toy:steer'))
    assert d.yes('active') is True
    run(d.finish('accepted'))
    assert len(requests) == 1
    assert inspector.get_calls()[0]['module_source'] == 'wb_toy_link'

    # Global Jev off or empty module id blocks plugin decisions too.
    service.save({'enabled': False})
    d_off = run(sdk.llm.decide({'prose': 'A gentle kiss.'}, {'active': noul('Is physical touch active?')},
                               module='wb_toy_link', step='toy:steer'))
    assert not d_off.answers and len(requests) == 1



