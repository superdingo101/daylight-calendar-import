"""Fail-closed contracts for bounded PR execution and release evidence."""
from __future__ import annotations

import copy
import json
from pathlib import Path
from types import SimpleNamespace

import pytest

from scripts import mutation_pr_gate as gate
from scripts import mutation_shards as shards


def counts(killed=97, survived=3, **extra):
    raw = dict.fromkeys(shards.FIELDS, 0)
    raw.update(killed=killed, survived=survived, **extra)
    raw['total'] = sum(raw.values())
    return raw


def mutant_map(modules, raw):
    result = {p: {} for p in modules}
    for status in shards.FIELDS:
        for n in range(raw[status]):
            result[modules[0]][f'{modules[0]}.test_{status}_{n}__mutmut_1'] = status
    return result


@pytest.fixture
def evidence(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    Path('mutation-baseline.json').write_text('{"minimum_score":95.22}')
    modules = [shards.SOURCE + 'sample.py']
    file = Path(modules[0]);file.parent.mkdir(parents=True);file.write_text('def check(): return 1\n')
    plan = dict(head_sha='a'*40, merge_base_sha='b'*40, selected_modules=modules,
                mode='scoped', may_execute_mutmut=True, max_modules_per_job=4,
                reason='Existing production module')
    def write(base=None, head=None):
        paths=[]
        for revision, raw in [('base',base or counts()),('head',head or counts())]:
            obj=dict(schema=1,complete=True,revision=revision,
                     sha=plan['head_sha'] if revision=='head' else plan['merge_base_sha'],
                     modules=modules, plan=plan,plan_sha256=shards.sha256(shards.canonical(plan)),
                     environment=shards.environment(),stats=raw,mutants=mutant_map(modules,raw))
            path=Path(f'bounded-{revision}.json');shards.write_json(path,obj);paths.append(path)
        return paths
    return plan, write


def test_same_scope_can_improve_without_any_verdict_cache(evidence):
    _, write=evidence
    report=gate.enforce(*write(head=counts(98,2)))
    assert report['head_score']==98 and report['base_score']==97


@pytest.mark.parametrize('raw',[counts(96,4),counts(95,5),counts(99,0,no_tests=1),counts(99,0,skipped=1),counts(99,0,segfault=1),counts(99,0,suspicious=1)])
def test_regression_floor_or_error_status_fails(evidence,raw):
    _,write=evidence
    with pytest.raises(ValueError):gate.enforce(*write(head=raw))


@pytest.mark.parametrize('key,value', [('complete',False),('sha','f'*40),('revision','base'),('modules',[]),('plan_sha256','f'*64),('mutants',{})])
def test_inconsistent_head_evidence_cannot_pass(evidence,key,value):
    _,write=evidence;paths=write()
    obj=json.loads(paths[1].read_text());obj[key]=value;shards.write_json(paths[1],obj)
    with pytest.raises(ValueError):gate.enforce(*paths)


def test_different_resolved_environments_cannot_pass(evidence):
    _,write=evidence;paths=write();obj=json.loads(paths[1].read_text())
    obj['environment']['details']['python']='3.14.999'
    obj['environment']['sha256']=shards.sha256(shards.canonical(obj['environment']['details']))
    shards.write_json(paths[1],obj)
    with pytest.raises(ValueError,match='environments'):gate.enforce(*paths)


def test_prepare_rejects_wrong_checkout(evidence,monkeypatch):
    plan,_=evidence;shards.write_json(gate.PLAN,plan)
    monkeypatch.setattr(shards,'git',lambda *args:'f'*40)
    with pytest.raises(ValueError,match='checkout'):gate.prepare('head')


def test_prepare_records_immutable_plan_and_configures_exact_scope(evidence,monkeypatch):
    plan,_=evidence;shards.write_json(gate.PLAN,plan)
    monkeypatch.setattr(shards,'git',lambda *args:plan['head_sha'])
    monkeypatch.setattr(shards,'source_paths',lambda:plan['selected_modules'])
    called=[];monkeypatch.setattr(gate,'configure_only_mutate',lambda modules:called.append(modules))
    result=gate.prepare('head')
    assert called==[plan['selected_modules']]
    assert result['plan_sha256']==shards.sha256(shards.canonical(plan))


@pytest.mark.parametrize('change',[{'selected_modules':[]},{'selected_modules':['../escape.py']},{'max_modules_per_job':0},{'may_execute_mutmut':False},{'head_sha':'main'}])
def test_invalid_execution_plan_is_rejected(evidence,change):
    plan,_=evidence;plan.update(change)
    with pytest.raises(ValueError):gate.validate_plan(plan)


def test_new_module_requires_comprehensive_validation(evidence,monkeypatch):
    from scripts import mutation_impact
    plan,_=evidence
    monkeypatch.setattr(mutation_impact,'plan',lambda *args,**kwargs:copy.deepcopy(plan))
    monkeypatch.setattr(gate.subprocess,'run',lambda *args,**kwargs:SimpleNamespace(returncode=1))
    result=gate.execution_plan('b'*40,'a'*40)
    assert result['mode']=='comprehensive-sharded' and result['may_execute_mutmut'] is False


def test_constants_only_change_requires_full_validation(evidence,monkeypatch):
    from scripts import mutation_impact
    plan,_=evidence;Path(plan['selected_modules'][0]).write_text('DEFAULT_VALUE=1\n')
    monkeypatch.setattr(mutation_impact,'plan',lambda *args,**kwargs:copy.deepcopy(plan))
    assert gate.execution_plan('b'*40,'a'*40)['mode']=='comprehensive-sharded'


def test_explicit_comprehensive_request_does_not_measure_test_coverage(evidence,monkeypatch):
    from scripts import mutation_impact
    plan,_=evidence;called=[]
    def planner(*args,**kwargs):called.append(kwargs);return copy.deepcopy(plan)
    monkeypatch.setattr(mutation_impact,'plan',planner)
    assert gate.execution_plan('b'*40,'a'*40,comprehensive=True)['mode']=='comprehensive-sharded'
    assert called==[{'measure':False}]


def test_scoped_record_accounts_for_each_mutant(evidence,monkeypatch):
    plan,write=evidence;obj=json.loads(write()[1].read_text());shards.write_json(gate.EXECUTION,obj)
    shards.write_json(shards.STATS,obj['stats'])
    monkeypatch.setattr(shards,'git',lambda *args:plan['head_sha'])
    monkeypatch.setattr(shards,'collect_mutants',lambda modules:obj['mutants'])
    result=gate.record('head')
    assert result['complete'] is True and result['stats']['total']==100


def test_release_proof_rejects_missing_comprehensive_evidence(evidence):
    from scripts import release_mutation_proof
    _,write=evidence;write()
    Path(shards.SOURCE+'manifest.json').write_text('{"version":"0.6.1"}')
    shards.write_json(shards.STATS,counts())
    with pytest.raises(OSError):release_mutation_proof.create('v0.6.1',sharded=True)
    assert not release_mutation_proof.PROOF_FILE.exists()
