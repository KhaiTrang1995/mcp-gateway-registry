import React, { useState, useMemo, useCallback } from 'react';
import {
  PlusIcon,
  MagnifyingGlassIcon,
  TrashIcon,
  ArrowLeftIcon,
  PencilIcon,
} from '@heroicons/react/24/outline';
import {
  useRateLimitDefinitions,
  setRateLimitDefinition,
  deleteRateLimitDefinition,
  setRateLimitEnabled,
  definitionId,
  RateLimitDefinition,
  TARGET_ENTITY_TYPES,
} from '../hooks/useRateLimits';
import DeleteConfirmation from './DeleteConfirmation';
import ListStateBoundary from './iam/ListStateBoundary';

interface IAMRateLimitsProps {
  onShowToast: (message: string, type: 'success' | 'error' | 'info') => void;
}

type View = 'list' | 'form';

// Extract a human-readable API error.
function apiError(err: any, fallback: string): string {
  return err?.response?.data?.detail || err?.message || fallback;
}

const inputBase =
  'w-full px-3 py-2 border rounded-md bg-white dark:bg-gray-800 text-gray-900 ' +
  'dark:text-white border-gray-300 dark:border-gray-600 focus:ring-2 focus:ring-blue-500';

const IAMRateLimits: React.FC<IAMRateLimitsProps> = ({ onShowToast }) => {
  const { definitions, isLoading, error, refetch } = useRateLimitDefinitions();
  const [searchQuery, setSearchQuery] = useState('');
  const [view, setView] = useState<View>('list');

  // Form state. axis drives which limit fields show.
  const [editingId, setEditingId] = useState<string | null>(null); // non-null => edit mode
  const [formAxis, setFormAxis] = useState<'caller' | 'target'>('caller');
  const [formEntityType, setFormEntityType] = useState('group');
  const [formName, setFormName] = useState('');
  const [formWindow, setFormWindow] = useState(60);
  const [formUserMax, setFormUserMax] = useState('');
  const [formAgentMax, setFormAgentMax] = useState('');
  const [formTargetMax, setFormTargetMax] = useState('');
  const [formFailClosed, setFormFailClosed] = useState(false);
  const [isSaving, setIsSaving] = useState(false);

  const [deleteTarget, setDeleteTarget] = useState<string | null>(null);

  const filtered = useMemo(() => {
    if (!searchQuery) return definitions;
    const q = searchQuery.toLowerCase();
    return definitions.filter(
      (d) => d.name.toLowerCase().includes(q) || d.entity_type.toLowerCase().includes(q),
    );
  }, [definitions, searchQuery]);

  const resetForm = useCallback(() => {
    setEditingId(null);
    setFormAxis('caller');
    setFormEntityType('group');
    setFormName('');
    setFormWindow(60);
    setFormUserMax('');
    setFormAgentMax('');
    setFormTargetMax('');
    setFormFailClosed(false);
  }, []);

  const openCreate = () => {
    resetForm();
    setView('form');
  };

  const openEdit = (d: RateLimitDefinition) => {
    setEditingId(definitionId(d));
    setFormAxis(d.axis);
    setFormEntityType(d.entity_type);
    setFormName(d.name);
    setFormWindow(d.window_seconds);
    setFormUserMax(d.user_max_requests != null ? String(d.user_max_requests) : '');
    setFormAgentMax(d.agent_max_requests != null ? String(d.agent_max_requests) : '');
    setFormTargetMax(d.max_requests != null ? String(d.max_requests) : '');
    setFormFailClosed(d.fail_closed);
    setView('form');
  };

  const handleSave = async () => {
    if (!formName.trim()) {
      onShowToast('Name is required', 'error');
      return;
    }
    const def: RateLimitDefinition = {
      axis: formAxis,
      entity_type: formAxis === 'caller' ? 'group' : formEntityType,
      name: formName.trim(),
      window_seconds: formWindow,
      fail_closed: formFailClosed,
      enabled: true,
      max_requests: formAxis === 'target' && formTargetMax ? Number(formTargetMax) : null,
      user_max_requests: formAxis === 'caller' && formUserMax ? Number(formUserMax) : null,
      agent_max_requests: formAxis === 'caller' && formAgentMax ? Number(formAgentMax) : null,
    };
    setIsSaving(true);
    try {
      await setRateLimitDefinition(def);
      onShowToast(`Rate limit ${editingId ? 'updated' : 'created'}`, 'success');
      resetForm();
      setView('list');
      refetch();
    } catch (err) {
      onShowToast(apiError(err, 'Failed to save rate limit'), 'error');
    } finally {
      setIsSaving(false);
    }
  };

  const handleToggle = async (d: RateLimitDefinition) => {
    try {
      await setRateLimitEnabled(definitionId(d), !d.enabled);
      onShowToast(`Rate limit ${d.enabled ? 'disabled' : 'enabled'}`, 'success');
      refetch();
    } catch (err) {
      onShowToast(apiError(err, 'Failed to toggle rate limit'), 'error');
    }
  };

  // DeleteConfirmation passes the entityPath and closes itself (via onCancel) on
  // success; we refetch + toast here and let a throw surface its error inline.
  const handleDelete = async (id: string) => {
    await deleteRateLimitDefinition(id);
    onShowToast('Rate limit deleted', 'success');
    refetch();
  };

  // Compact "limit per window" summary for a row.
  const limitSummary = (d: RateLimitDefinition): string => {
    const w = `${d.window_seconds}s`;
    if (d.axis === 'caller') {
      const parts: string[] = [];
      if (d.user_max_requests != null) parts.push(`user ${d.user_max_requests}`);
      if (d.agent_max_requests != null) parts.push(`agent ${d.agent_max_requests}`);
      return `${parts.join(', ')} / ${w}`;
    }
    return `${d.max_requests} / ${w}`;
  };

  if (view === 'form') {
    const isCaller = formAxis === 'caller';
    return (
      <div className="space-y-6">
        <div className="flex items-center justify-between">
          <h2 className="text-lg font-semibold text-gray-900 dark:text-white">
            IAM &gt; Rate Limits &gt; {editingId ? 'Edit' : 'Create'}
          </h2>
          <button
            onClick={() => { resetForm(); setView('list'); }}
            className="flex items-center text-sm text-gray-500 dark:text-gray-400 hover:text-gray-700 dark:hover:text-gray-200"
          >
            <ArrowLeftIcon className="h-4 w-4 mr-1" />
            Back to List
          </button>
        </div>

        <div className="space-y-4 max-w-lg">
          <div>
            <label className="block text-sm text-gray-600 dark:text-gray-400 mb-1">Axis</label>
            <select
              value={formAxis}
              onChange={(e) => setFormAxis(e.target.value as 'caller' | 'target')}
              disabled={!!editingId}
              className={inputBase}
            >
              <option value="caller">caller (a group of users/agents)</option>
              <option value="target">target (an MCP server / A2A agent)</option>
            </select>
          </div>

          {!isCaller && (
            <div>
              <label className="block text-sm text-gray-600 dark:text-gray-400 mb-1">Target type</label>
              <select
                value={formEntityType}
                onChange={(e) => setFormEntityType(e.target.value)}
                disabled={!!editingId}
                className={inputBase}
              >
                {TARGET_ENTITY_TYPES.map((t) => (
                  <option key={t} value={t}>{t}</option>
                ))}
              </select>
            </div>
          )}

          <div>
            <label className="block text-sm text-gray-600 dark:text-gray-400 mb-1">
              {isCaller ? 'Group name' : 'Target name (server path / agent path)'}
            </label>
            <input
              type="text"
              value={formName}
              onChange={(e) => setFormName(e.target.value)}
              disabled={!!editingId}
              placeholder={isCaller ? 'e.g. rate-limited-testers' : 'e.g. mcpgw'}
              className={inputBase}
            />
          </div>

          <div>
            <label className="block text-sm text-gray-600 dark:text-gray-400 mb-1">
              Window (seconds) — 1 to 86400
            </label>
            <input
              type="number"
              value={formWindow}
              onChange={(e) => setFormWindow(Number(e.target.value))}
              disabled={!!editingId}
              className={inputBase}
            />
            <p className="mt-1 text-xs text-gray-400">
              Editing a definition keeps its axis/type/name/window (they form its id). Change those by
              deleting and recreating.
            </p>
          </div>

          {isCaller ? (
            <div className="grid grid-cols-2 gap-4">
              <div>
                <label className="block text-sm text-gray-600 dark:text-gray-400 mb-1">
                  User max/window
                </label>
                <input
                  type="number"
                  value={formUserMax}
                  onChange={(e) => setFormUserMax(e.target.value)}
                  placeholder="e.g. 25"
                  className={inputBase}
                />
              </div>
              <div>
                <label className="block text-sm text-gray-600 dark:text-gray-400 mb-1">
                  Agent max/window
                </label>
                <input
                  type="number"
                  value={formAgentMax}
                  onChange={(e) => setFormAgentMax(e.target.value)}
                  placeholder="e.g. 15"
                  className={inputBase}
                />
              </div>
              <p className="col-span-2 text-xs text-gray-400">
                Set at least one. On windows ≤ 60s a floor applies (user ≥ 20/min, agent ≥ 10/min by
                default) and values below it are rejected.
              </p>
            </div>
          ) : (
            <div>
              <label className="block text-sm text-gray-600 dark:text-gray-400 mb-1">Max requests/window</label>
              <input
                type="number"
                value={formTargetMax}
                onChange={(e) => setFormTargetMax(e.target.value)}
                placeholder="e.g. 500"
                className={inputBase}
              />
            </div>
          )}

          <label className="flex items-center gap-2 text-sm text-gray-600 dark:text-gray-400">
            <input
              type="checkbox"
              checked={formFailClosed}
              onChange={(e) => setFormFailClosed(e.target.checked)}
            />
            Fail closed (deny on backend error — security-critical limits only)
          </label>

          <div className="flex gap-3 pt-2">
            <button
              onClick={handleSave}
              disabled={isSaving}
              className="px-4 py-2 bg-blue-600 text-white rounded-md hover:bg-blue-700 disabled:opacity-50"
            >
              {isSaving ? 'Saving…' : editingId ? 'Update' : 'Create'}
            </button>
            <button
              onClick={() => { resetForm(); setView('list'); }}
              className="px-4 py-2 border border-gray-300 dark:border-gray-600 rounded-md text-gray-700 dark:text-gray-200"
            >
              Cancel
            </button>
          </div>
        </div>
      </div>
    );
  }

  return (
    <div className="space-y-6">
      <div className="flex items-center justify-between">
        <h2 className="text-lg font-semibold text-gray-900 dark:text-white">IAM &gt; Rate Limits</h2>
        <button
          onClick={openCreate}
          className="flex items-center px-3 py-2 bg-blue-600 text-white text-sm rounded-md hover:bg-blue-700"
        >
          <PlusIcon className="h-4 w-4 mr-1" />
          New Rate Limit
        </button>
      </div>

      <div className="relative max-w-md">
        <MagnifyingGlassIcon className="absolute left-3 top-1/2 -translate-y-1/2 h-4 w-4 text-gray-400" />
        <input
          type="text"
          value={searchQuery}
          onChange={(e) => setSearchQuery(e.target.value)}
          placeholder="Search by name or type"
          className={`${inputBase} pl-9`}
        />
      </div>

      <ListStateBoundary
        isLoading={isLoading}
        error={error}
        isEmpty={filtered.length === 0}
        emptyMessage="No rate-limit definitions. Create one to cap tool/agent usage."
      >
        <div className="overflow-x-auto">
          <table className="min-w-full text-sm">
            <thead>
              <tr className="text-left text-gray-500 dark:text-gray-400 border-b border-gray-200 dark:border-gray-700">
                <th className="py-2 pr-4">Axis</th>
                <th className="py-2 pr-4">Type</th>
                <th className="py-2 pr-4">Name</th>
                <th className="py-2 pr-4">Limit</th>
                <th className="py-2 pr-4">State</th>
                <th className="py-2 pr-4">Actions</th>
              </tr>
            </thead>
            <tbody>
              {filtered.map((d) => {
                const id = definitionId(d);
                return (
                  <tr key={id} className="border-b border-gray-100 dark:border-gray-800">
                    <td className="py-2 pr-4">{d.axis}</td>
                    <td className="py-2 pr-4">{d.entity_type}</td>
                    <td className="py-2 pr-4 font-mono text-xs">{d.name}</td>
                    <td className="py-2 pr-4">{limitSummary(d)}</td>
                    <td className="py-2 pr-4">
                      <span className={d.enabled ? 'text-green-600' : 'text-gray-400'}>
                        {d.enabled ? 'enabled' : 'disabled'}
                      </span>
                      {d.fail_closed && <span className="ml-2 text-xs text-amber-600">fail-closed</span>}
                    </td>
                    <td className="py-2 pr-4">
                      <div className="flex items-center gap-3">
                        <button
                          onClick={() => openEdit(d)}
                          className="text-gray-500 hover:text-blue-600"
                          title="Edit"
                        >
                          <PencilIcon className="h-4 w-4" />
                        </button>
                        <button
                          onClick={() => handleToggle(d)}
                          className="text-xs text-gray-500 hover:text-blue-600"
                          title={d.enabled ? 'Disable' : 'Enable'}
                        >
                          {d.enabled ? 'Disable' : 'Enable'}
                        </button>
                        <button
                          onClick={() => setDeleteTarget(id)}
                          className="text-gray-500 hover:text-red-600"
                          title="Delete"
                        >
                          <TrashIcon className="h-4 w-4" />
                        </button>
                      </div>
                    </td>
                  </tr>
                );
              })}
            </tbody>
          </table>
        </div>
      </ListStateBoundary>

      {deleteTarget && (
        <DeleteConfirmation
          entityType="rate-limit"
          entityName={deleteTarget}
          entityPath={deleteTarget}
          onConfirm={handleDelete}
          onCancel={() => setDeleteTarget(null)}
        />
      )}
    </div>
  );
};

export default IAMRateLimits;
