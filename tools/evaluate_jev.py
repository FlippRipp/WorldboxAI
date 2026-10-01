"""Opt-in, paid evaluation on fictional cases; never loads an adventure/save.

python -m tools.evaluate_jev --list
python -m tools.evaluate_jev --live --output _scratch/jev-evaluation.json
"""
import argparse
import asyncio
import json
from pathlib import Path
import statistics
import time

from backend.engine.jev import choice, noul, Uncertain, INPUT_USD_PER_MILLION, PRICE_DATE
from backend.engine.llm import LLMService
from backend.engine.provider_manager import ProviderManager


CASES = [
    {"id": "presence-mentioned", "state": "Mira is alone in the locked cabin. She remembers Borin, who is at home in another town.",
     "questions": {"present": noul("Is Borin physically present with Mira now?")}, "expected": {"present": False}},
    {"id": "presence-pronoun", "state": "Borin enters Mira's cabin. He pulls up a chair beside her and begins talking.",
     "questions": {"present": noul("Is Borin physically present with Mira now?")}, "expected": {"present": True}},
    {"id": "rpg-dialogue", "state": "Mira sits safely at home. The player says: Hello, Borin. There is no conflict or social goal.",
     "questions": {"substantive": noul("Is this a substantive action with a contested outcome or mechanical consequence?")}, "expected": {"substantive": False}},
    {"id": "rpg-persuasion", "state": "A hostile guard refuses entry. Mira attempts to persuade him to admit her despite the ban.",
     "questions": {"substantive": noul("Is this a substantive action with a contested outcome or mechanical consequence? Contested social attempts count.")}, "expected": {"substantive": True}},
    {"id": "practice-specific-skill", "state": "Mira uses a fire dance to entertain the crowd. Fire lights a small flame; Fire Dance is a choreographed performance with flames.",
     "questions": {"skill": choice("Which skill is being practiced?", {"fire": "Fire: kindle a small flame", "fire_dance": "Fire Dance: perform choreography with flames", "none": "No skill applies"})}, "expected": {"skill": "fire_dance"}},
    {"id": "failure-grounding", "state": "World rule: no creature can fly here. Mira has no wings, spells, or flying equipment. She attempts to fly by wishing.",
     "questions": {"feasibility": choice("Rate this attempt: 1-2 impossible, 3-4 beyond capability, 5-6 challenging, 7-8 plausible, 9-10 near certain.", {str(i): f"{i}/10" for i in range(1, 11)})}, "expected": {"feasibility": ["1", "2"]}},
    {"id": "xp-routine", "module": "wb_core_rpg", "baseline": "reader",
     "state": "Mira safely repeats a mastered candle-lighting trick with no effort, risk, ingenuity or learning. Rules: award zero for routine repetition, 2-5 for marginal merit, around 10 for solid success, 15-25 for a hard feat, up to 50 for extraordinary events.",
     "questions": {"xp": choice("Choose the XP award under these rules.", {**{str(i): f"{i} XP" for i in range(51)}, "outside": "Outside this range"})}, "expected": {"xp": "0"}},
    {"id": "external-boon", "screen": True,
     "state": "The hearth goddess permanently grants Mira Emberkiss: she can kindle any flame she can see. This is a new skill, not practice or a temporary effect.",
     "questions": {"events": noul("Does this scene contain an externally imposed lasting skill change?")}, "expected": {"events": True}},
    {"id": "external-temporary", "screen": True,
     "state": "Mira is briefly dazzled by a flash. Her sight returns moments later. No abilities are granted, removed or altered permanently.",
     "questions": {"events": noul("Does this scene contain an externally imposed lasting skill change? Exclude temporary conditions.")}, "expected": {"events": False}},
    {"id": "npc-rename", "screen": True,
     "state": "Record n1 is named The Hooded Stranger. In this scene she reveals her true name is Veyra.",
     "questions": {"changes": noul("Does n1 have a reportable record change? Revealing a true name counts.")}, "expected": {"changes": True}},
    {"id": "npc-unchanged", "screen": True,
     "state": "Borin briefly yawns and then returns to the exact same work. No new facts, names, lasting changes or noteworthy deeds occur.",
     "questions": {"changes": noul("Does Borin have a reportable lasting record change or noteworthy addition to his notes?")}, "expected": {"changes": False}},
    {"id": "npc-reuse", "screen": True,
     "state": "Mira needs a blacksmith. The existing NPC bank includes Borin, an available blacksmith in her town who can serve this need. There is no other unmet need.",
     "questions": {"new": noul("Does the story need a NEW character for a concrete need no existing character can serve?")}, "expected": {"new": False}},
    {"id": "npc-travel", "state": "Off-screen Borin promised to bring Mira the antidote now. He knows her location, has the antidote, and is free to travel.",
     "questions": {"travel": noul("Does Borin have a concrete narrative reason to travel toward Mira now?")}, "expected": {"travel": True}},
]


def matches(actual, expected):
    return all(actual.get(k) in v if isinstance(v, list) else actual.get(k) == v for k, v in expected.items())


async def evaluate(providers_dir):
    manager = ProviderManager(providers_dir)
    service = manager.decisions
    if not service.public_config()["api_key_set"]:
        raise SystemExit("Configure a TypeSafe key in Model Settings before running live evaluation.")
    llm = LLMService(mode="live")
    manager.set_llm_service(llm)
    rows = []
    for case in CASES:
        started = time.perf_counter()
        result = await service.decide(case['state'], case['questions'], module=case.get('module', 'wb_npc_system'),
                                      step='evaluation:' + case['id'], test=True)
        accepted, actual = True, {}
        try:
            for key, question in case['questions'].items():
                value = result.pick(key) if question['type'] == 'choice' else result.yes(key)
                if case.get('screen') and value is False and not result.no_work(key):
                    raise Uncertain('not certain enough to skip')
                actual[key] = value
        except Uncertain:
            accepted = False
        await result.finish('accepted' if accepted else 'fallback')
        jev_ms = round((time.perf_counter() - started) * 1000, 1)
        model = llm.reader_model if case.get('baseline') == 'reader' else llm.module_fast_model
        started = time.perf_counter()
        baseline = {}
        usage = {}
        error = None
        try:
            raw, _, usage = await llm.simple_completion(
                [{'role': 'user', 'content': 'Evaluate the state and questions. Return only a JSON object mapping question IDs to their answers: a boolean for noul, an exact option key for choice.\n' + json.dumps(
                    {'state': case['state'], 'questions': case['questions']}, ensure_ascii=False)}],
                model=model, response_format={'type': 'json_object'}, return_usage=True)
            baseline = llm._parse_json_object(raw)
        except Exception as exc:
            error = type(exc).__name__
        rows.append({'case': case['id'], 'expected': case['expected'], 'jev_model': result.model,
            'accepted': accepted, 'jev_answers': actual, 'jev_correct': matches(actual, case['expected']) if accepted else None,
            'jev_ms': jev_ms, 'jev_usage': result.usage,
            'jev_estimated_usd': (result.usage['input_tokens'] * INPUT_USD_PER_MILLION / 1_000_000
                                  if 'input_tokens' in result.usage else None),
            'baseline_model': model, 'baseline_ms': round((time.perf_counter() - started) * 1000, 1),
            'baseline_answers': baseline, 'baseline_correct': matches(baseline, case['expected']),
            'baseline_usage': usage, 'baseline_reported_usd': usage.get('cost'), 'baseline_error': error})
    accepted = [r for r in rows if r['accepted']]
    return {'pricing_date': PRICE_DATE, 'cases': rows,
            'summary': {'count': len(rows), 'accepted': len(accepted),
                'accepted_correct': sum(r['jev_correct'] for r in accepted),
                'fallback_fraction': 1 - len(accepted) / len(rows),
                'jev_median_ms': statistics.median(r['jev_ms'] for r in rows),
                'baseline_median_ms': statistics.median(r['baseline_ms'] for r in rows),
                'jev_known_estimated_usd': sum(r['jev_estimated_usd'] or 0 for r in rows),
                'jev_unknown_cost_calls': sum(r['jev_estimated_usd'] is None for r in rows)},
            'limitations': 'Small fictional primitive-level evaluation, not end-to-end gameplay or calibrated accuracy. Baseline monetary cost is null when not reported; no inferred savings.'}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--live', action='store_true', help='Make paid Jev and existing-model calls on fictional cases.')
    parser.add_argument('--list', action='store_true', help='List fictional cases without API calls.')
    parser.add_argument('--providers-dir', default='data/providers')
    parser.add_argument('--output', type=Path)
    args = parser.parse_args()
    if args.list:
        print(json.dumps(CASES, indent=2))
        return
    if not args.live:
        parser.error('Use --list for offline inspection or --live to opt into paid evaluation.')
    report = asyncio.run(evaluate(args.providers_dir))
    text = json.dumps(report, indent=2)
    if args.output:
        args.output.parent.mkdir(parents=True, exist_ok=True)
        args.output.write_text(text, encoding='utf-8')
    else:
        print(text)


if __name__ == '__main__':
    main()
