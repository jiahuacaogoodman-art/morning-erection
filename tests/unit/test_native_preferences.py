"""Settings tests use the installed Pi runtime and its own files, reload and trust store."""
import json
from pathlib import Path

import pytest
from test_native_pi import native as real_native

from wakecore.adapters.agent_runtime.native import NativePiError

native = real_native


def read(host):
    return host.control('native_preferences')


def update(host, patch, scope='global', snapshot=None):
    snapshot = snapshot or read(host)
    return host.control('update_native_preferences', {'scope': scope, 'patch': patch,
        'revision': snapshot['revisions'][scope], 'workspace': snapshot['workspace']})


def test_native_preferences_read_does_not_start_agent_or_create_parallel_settings(native):
    host, _ = native
    data = read(host)
    assert not host.agent_ready
    assert len(data['fields']) == 35
    fields = {field['key']: field for field in data['fields']}
    assert fields['retry.maxRetries']['value'] == 3
    assert fields['compaction.enabled']['value'] is True
    assert fields['cacheWarming']['globalOnly']
    assert [option['value'] for option in fields['defaultThinkingLevel']['options']] == ['off', 'minimal', 'low', 'medium', 'high', 'xhigh', 'max']
    assert not (Path(host.agent_dir) / 'settings.json').exists()
    assert data['revisions']['global']


def test_preferences_preserve_unknown_native_fields_and_session_on_reload_and_restart(native):
    host, conversation = native
    file = Path(host.agent_dir) / 'settings.json'
    file.parent.mkdir(parents=True)
    file.write_text(json.dumps({'futureNativeFeature': {'preserve': True}, 'retry': {'provider': {'future': 123}}}))
    result = update(host, {'compaction.reserveTokens': 8192, 'retry.maxRetries': 7, 'steeringMode': 'all', 'images.blockImages': True})
    assert result['saved'] and result['applied']
    assert json.loads(file.read_text())['retry']['provider']['future'] == 123
    host.rpc('bash', {'command': 'printf NATIVE_SETTINGS_HISTORY'})
    state = conversation.snapshot()['state']
    assert state['steeringMode'] == 'all'
    before = host.rpc('get_messages')['messages']
    result = update(host, {'transport': 'sse', 'retry.maxRetries': 5, 'images.autoResize': False, 'defaultThinkingLevel': 'max'})
    assert result['applied']
    assert host.rpc('get_state')['sessionId'] == state['sessionId']
    assert host.rpc('get_state')['thinkingLevel'] == state['thinkingLevel']
    assert host.rpc('get_messages')['messages'] == before
    assert json.loads(file.read_text())['futureNativeFeature'] == {'preserve': True}
    host.close()
    assert host.rpc('get_state')['sessionId'] == state['sessionId']
    assert next(f for f in read(host)['fields'] if f['key'] == 'retry.maxRetries')['value'] == 5
    assert next(f for f in read(host)['fields'] if f['key'] == 'defaultThinkingLevel')['value'] == 'max'


def test_settings_conflict_invalid_patch_and_wrong_workspace_do_not_write(native):
    host, _ = native
    before = read(host)
    update(host, {'retry.maxRetries': 4})
    file = Path(host.agent_dir) / 'settings.json'
    saved = file.read_text()
    with pytest.raises(NativePiError, match='别处更新'):
        update(host, {'retry.maxRetries': 9}, snapshot=before)
    with pytest.raises(NativePiError, match='有效'):
        update(host, {'compaction.reserveTokens': -1, 'retry.maxRetries': 10})
    with pytest.raises(NativePiError, match='不支持'):
        update(host, {'httpProxy': 'http://unexpected.example'})
    wrong = {**read(host), 'workspace': '/another-workspace'}
    with pytest.raises(NativePiError, match='工作区已变化'):
        update(host, {'retry.maxRetries': 10}, snapshot=wrong)
    assert file.read_text() == saved


def test_native_project_trust_override_and_reset_to_inherited(native):
    host, _ = native
    update(host, {'retry.maxRetries': 6})
    with pytest.raises(NativePiError, match='信任'):
        update(host, {'retry.maxRetries': 2}, scope='project')
    data = read(host)
    host.control('set_workspace_trust', {'workspace': data['workspace'], 'trusted': True})
    result = update(host, {'retry.maxRetries': 2}, scope='project')
    field = next(f for f in result['fields'] if f['key'] == 'retry.maxRetries')
    assert field['source'] == 'project' and field['value'] == 2
    project_file = host.workspace / '.pi/settings.json'
    assert json.loads(project_file.read_text())['retry']['maxRetries'] == 2
    result = update(host, {'retry.maxRetries': None}, scope='project')
    field = next(f for f in result['fields'] if f['key'] == 'retry.maxRetries')
    assert field['source'] == 'global' and field['value'] == 6
    with pytest.raises(NativePiError, match='不支持'):
        update(host, {'cacheWarming': 'idle'}, scope='project')
    host.control('set_workspace_trust', {'workspace': data['workspace'], 'trusted': False})
    assert read(host)['projectTrusted'] is False


def test_extra_skill_source_uses_real_resource_loader(native, tmp_path):
    host, _ = native
    skill = tmp_path / 'native-settings-skill'
    skill.mkdir()
    (skill / 'SKILL.md').write_text('---\nname: prefs-contract\ndescription: Verify native resource paths.\n---\nRead the target.\n')
    host.control('start_agent')
    result = update(host, {'skills': [str(skill)]})
    assert result['applied']
    assert any(item['name'] == 'prefs-contract' for item in host.control('resources')['skills'])
    update(host, {'skills': None})
    assert not any(item['name'] == 'prefs-contract' for item in host.control('resources')['skills'])


def test_revoking_native_workspace_trust_removes_project_resources(native):
    host, _ = native
    skill = host.workspace / '.pi/skills/workspace-contract'
    skill.mkdir(parents=True)
    (skill / 'SKILL.md').write_text('---\nname: workspace-only-contract\ndescription: Check project trust.\n---\nRead only.\n')
    host.control('set_workspace_trust', {'workspace': str(host.workspace), 'trusted': True})
    assert any(item['name'] == 'workspace-only-contract' for item in host.control('resources')['skills'])
    host.control('set_workspace_trust', {'workspace': str(host.workspace), 'trusted': False})
    assert not any(item['name'] == 'workspace-only-contract' for item in host.control('resources')['skills'])
