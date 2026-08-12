import { useState } from 'react'
import { runTextQuery } from '../services/api'

export function useQuery() {
  const [status, setStatus] = useState('idle')
  const [result, setResult] = useState(null)
  const [error, setError] = useState(null)

  const submitText = async (query, dialect = 'Oracle') => {
    setStatus('loading')
    setError(null)
    setResult(null)
    try {
      const data = await runTextQuery(query, dialect)
      if (data?.timings_ms) {
        // Mirrors the backend's own [TIMING] log lines so the per-stage
        // breakdown (including db_execution, the Oracle query time) is
        // visible in the browser console too, not just server-side stdout.
        const total = Object.values(data.timings_ms).reduce((a, b) => a + b, 0)
        console.log(
          `[TIMING] query="${query}" total=${total.toFixed(1)}ms`,
          data.timings_ms,
        )
      }
      setResult(data)
      setStatus('success')
    } catch (e) {
      setError(e instanceof Error ? e.message : 'Unknown error')
      setStatus('error')
    }
  }

  const reset = () => {
    setStatus('idle')
    setResult(null)
    setError(null)
  }

  return { status, result, error, submitText, reset }
}
