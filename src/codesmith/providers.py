"""Small, deterministic routing layer above qualified agent adapters.

Fallback order is explicit task configuration, not inferred from model names. A quota
switch is sticky for the task; exhausted profiles cannot cause a retry cycle. Unknown
headroom never predicts a quota failure. All state mutations belong to the coordinator.
"""
import time
from datetime import datetime, timezone
from . import qualification


def model_for(task, name, profiles):
    return task.get('provider_models', {}).get(name,
        (task.get('model', '') if name == task['agent'] else '') or profiles[name].get('model', ''))


def quota_scope(name, profile):
    return name if profile['kind'] == 'command' else profile['kind']


class ProviderRouting:
    def provider_task(self, task):
        from .engine import EngineStop
        names = [task['agent']] + task.get('fallback_agents', [])
        st = self.st(task['id'])
        selected = st.get('selected_provider', task['agent'])
        if selected not in names:
            selected = task['agent']
        qualifications = st.get('provider_qualifications', {})
        exhausted = self.run.state.get('provider_quota', {})
        for name in names[names.index(selected):]:
            profile = self.wf['agents'][name]
            entry = qualifications.get(name)
            # Legacy runs without fallback retain their original qualification path.
            if entry is None and not task.get('fallback_agents') and name == task['agent']:
                return task
            if entry is None or qualification.required(task, profile) - set(entry['capabilities']):
                continue
            if exhausted.get(quota_scope(name, profile), {}).get('until', 0) > time.time():
                continue
            if name != st.get('selected_provider', task['agent']):
                self.record_provider_switch(task, name, 'previous provider unavailable or unqualified')
            st['provider_current'] = {
                'profile': name, 'kind': profile['kind'], 'model': model_for(task, name, self.wf['agents']),
                'complexity': task.get('complexity', 'standard'),
                'reason': st.get('provider_history', [{}])[-1].get('reason', 'configured preference')}
            st['qualified'] = entry['capabilities']
            st['qualification_key'] = entry['key']
            return dict(task, agent=name, model=model_for(task, name, self.wf['agents']))
        raise EngineStop(f"no qualified available provider for '{task['id']}'; saved work retained. "
                         "Inspect provider-selection events; resume after quota cooldown or replan profiles")

    def step_to_fallback(self, task, reason):
        """After a transient provider failure: move to the next qualified, available fallback,
        if the task has one. Returns its name, or None when there is nowhere to go."""
        st = self.st(task['id'])
        names = [task['agent']] + task.get('fallback_agents', [])
        current = st.get('selected_provider', task['agent'])
        if current not in names:
            current = task['agent']
        qualifications = st.get('provider_qualifications', {})
        exhausted = self.run.state.get('provider_quota', {})
        for name in names[names.index(current) + 1:]:
            profile = self.wf['agents'][name]
            entry = qualifications.get(name)
            if entry is None or qualification.required(task, profile) - set(entry['capabilities']):
                continue
            if exhausted.get(quota_scope(name, profile), {}).get('until', 0) > time.time():
                continue
            self.record_provider_switch(task, name, reason)
            return name
        return None

    def record_provider_switch(self, task, name, reason):
        st = self.st(task['id'])
        previous = st.get('selected_provider', task['agent'])
        selection = {'from': previous, 'to': name, 'reason': reason,
                     'model': model_for(task, name, self.wf['agents']),
                     'complexity': task.get('complexity', 'standard'), 'timestamp': datetime.now(timezone.utc).isoformat()}
        st.setdefault('provider_history', []).append(selection)
        st.update(selected_provider=name, session_id=None, provider_switched=True)
        self.run.save()
        self.run.event('provider-selection', task=task['id'], **selection)
        self.crash('provider:selection-recorded')

    def provider_quota(self, task, result):
        """Persist explicit quota evidence, without charging a task/protocol attempt."""
        from .engine import EngineStop
        st = self.st(task['id'])
        name = st.get('selected_provider', task['agent'])
        kind = quota_scope(name, self.wf['agents'][name])
        evidence = {'profile': name, 'reason': result.error[:2000],
                    'observed_at': datetime.now(timezone.utc).isoformat(), 'until': time.time() + 300}
        self.run.state.setdefault('provider_quota', {})[kind] = evidence
        st['session_id'] = None
        st.pop('pending_provider_quota', None)
        self.run.save()
        self.run.event('provider-quota', task=task['id'], **evidence)
        names = [task['agent']] + task.get('fallback_agents', [])
        # A task moves only forward through its authorized preference list.
        for next_name in names[names.index(name) + 1:]:
            q = st.get('provider_qualifications', {}).get(next_name)
            profile = self.wf['agents'][next_name]
            if q and not (qualification.required(task, profile) - set(q['capabilities'])):
                if self.run.state['provider_quota'].get(quota_scope(next_name, profile), {}).get('until', 0) > time.time():
                    continue
                self.record_provider_switch(task, next_name, 'confirmed provider quota')
                return
        raise EngineStop(f"quota exhausted for '{task['id']}' on '{name}'; no qualified available "
                         "fallback. Saved work retained; resume after cooldown or replan profiles")
