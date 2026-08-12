const STAGE_LABELS = {
  exact_match_lookup: 'Exact-match lookup',
  retrieval: 'Schema retrieval',
  selection: 'Table selection',
  llm_generation: 'SQL generation (LLM)',
  validation: 'SQL validation',
  db_execution: 'Query execution (Oracle)',
}

function stageLabel(key) {
  return STAGE_LABELS[key] || key
}

export default function TimingsPanel({ timings }) {
  if (!timings || Object.keys(timings).length === 0) return null

  const entries = Object.entries(timings)
  const total = entries.reduce((sum, [, ms]) => sum + ms, 0)
  const dbExecutionMs = timings.db_execution

  return (
    <div className="rounded-xl border border-gray-200 dark:border-gray-700 bg-white dark:bg-gray-800 shadow-sm transition-colors duration-200">
      <div className="flex items-center justify-between border-b border-gray-100 dark:border-gray-700 px-4 py-3">
        <span className="text-sm font-semibold text-gray-700 dark:text-gray-200">⏱ Timings</span>
        <span className="rounded-full bg-gray-100 dark:bg-gray-700 px-2.5 py-0.5 text-xs font-medium text-gray-600 dark:text-gray-300">
          total {total.toFixed(0)} ms
        </span>
      </div>

      <div className="divide-y divide-gray-100 dark:divide-gray-700">
        {entries.map(([stage, ms]) => {
          const pct = total > 0 ? (ms / total) * 100 : 0
          const isDbExec = stage === 'db_execution'
          return (
            <div key={stage} className="flex items-center gap-3 px-4 py-2">
              <span
                className={`w-44 shrink-0 text-xs font-medium ${
                  isDbExec
                    ? 'text-indigo-700 dark:text-indigo-300'
                    : 'text-gray-500 dark:text-gray-400'
                }`}
              >
                {stageLabel(stage)}
              </span>
              <div className="h-1.5 flex-1 overflow-hidden rounded-full bg-gray-100 dark:bg-gray-700">
                <div
                  className={`h-full rounded-full ${
                    isDbExec ? 'bg-indigo-500' : 'bg-gray-400 dark:bg-gray-500'
                  }`}
                  style={{ width: `${Math.max(pct, 2)}%` }}
                />
              </div>
              <span
                className={`w-20 shrink-0 text-right text-xs font-mono ${
                  isDbExec
                    ? 'font-semibold text-indigo-700 dark:text-indigo-300'
                    : 'text-gray-600 dark:text-gray-300'
                }`}
              >
                {ms.toFixed(1)} ms
              </span>
            </div>
          )
        })}
      </div>

      {dbExecutionMs != null && (
        <div className="border-t border-gray-100 dark:border-gray-700 px-4 py-2 text-xs text-gray-400 dark:text-gray-500">
          Query execution against Oracle took <span className="font-semibold text-indigo-600 dark:text-indigo-300">{dbExecutionMs.toFixed(1)} ms</span>.
          Full connect/execute/fetch breakdown is in the backend log ([TIMING] execute_query …).
        </div>
      )}
    </div>
  )
}
