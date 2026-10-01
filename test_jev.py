import asyncio
import json
from types import SimpleNamespace

import httpx
import pytest

from backend.engine.jev import JevService, choice, noul, Uncertain, fallback_generate
from backend.engine import jev_tasks, nsfw
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
        'npc_enabled': True, 'model': 'jev-latest', 'api_key_set': True}
    assert 'secret' not in json.dumps(reloaded.public_config())
    reloaded.save({'remove_api_key': True})
    assert not reloaded.public_config()['api_key_set']
    with pytest.raises(ValueError):
        reloaded.save({'enabled': 'yes'})


@pytest.mark.parametrize('mode,updates', [('mock', {}), ('live', {'enabled': False}),
    ('live', {'rpg_enabled': False}), ('live', {'remove_api_key': True})])
def test_disabled_missing_and_mock_never_call_network(tmp_path, mode, updates):
    service, _, requests, _, _ = setup_jev(tmp_path)
    service.save(updates)
    result = run(service.decide('story', {'ok': noul('yes?')}, module=jev_tasks.RPG, step='test', mode=mode))
    assert not result.answers and not requests
    with pytest.raises(Uncertain):
        result.yes('ok')


def test_nsfw_routing_bypasses_jev(tmp_path):
    service, _, requests, _, _ = setup_jev(tmp_path)
    async def task():
        with nsfw.operation({'nsfw': {**nsfw.empty(), 'enabled': True}}, SimpleNamespace(nsfw_model='special')):
            return await service.decide('private story', {'ok': noul('yes?')}, module=jev_tasks.NPC, step='test')
    assert not run(task()).answers
    assert not requests


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


def test_projection_preserves_decision_keys_and_blocks_changed_schema(tmp_path):
    service, _, requests, _, _ = setup_jev(tmp_path, {'ok': 1})
    class Projector:
        enabled = False
        async def messages(self, messages):
            content = json.loads(messages[0]['content'])
            content['state'] = 'Projected non-graphic facts'
            if getattr(self, 'tamper', False):
                content['questions'] = {}
            return [{'role': 'user', 'content': json.dumps(content)}]
    projector = Projector()
    async def task():
        token = nsfw._operation.set(projector)
        try:
            d = await service.decide('original', {'ok': noul('yes?')}, module=jev_tasks.RPG, step='test')
            await d.finish('accepted')
            projector.tamper = True
            failed = await service.decide('original', {'ok': noul('yes?')}, module=jev_tasks.RPG, step='test')
            assert not failed.answers
        finally:
            nsfw._operation.reset(token)
    run(task())
    assert len(requests) == 1 and requests[0]['state'] == 'Projected non-graphic facts'


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
