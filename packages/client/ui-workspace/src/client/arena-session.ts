/** Optional initial Session selection for an embedded, externally driven Arena pane. */
import type { ISessions, SessionListState } from '@deepseek-ai/dsh-api-session-controller/client'
import type { SessionId } from '@deepseek-ai/dsh-session/types'

/**
 * Open the requested Session once its authoritative list entry exists; never create one.
 * @param search - Browser query containing an optional `arenaSession` identity.
 * @param source - Authoritative Session list; subscribes before checking the initial snapshot.
 * @param open - Existing Workspace navigation action.
 * @returns Disposer that also prevents late selection after the owning fiber is unloaded.
 */
export function followArenaSession(
  search: string,
  source: Pick<ISessions['list'], 'subscribe'> & { getSnapshot(): Pick<SessionListState, 'ids'> },
  open: (sessionId: SessionId) => void,
): () => void {
  const requested = new URLSearchParams(search).get('arenaSession')
  if (!requested) return () => {}
  let selected = false
  let disposed = false
  const check = (): void => {
    if (disposed || selected) return
    const id = source.getSnapshot().ids.find(candidate => candidate === requested)
    if (id === undefined) return
    selected = true
    open(id)
  }
  const unsubscribe = source.subscribe(check)
  check()
  return () => {
    disposed = true
    unsubscribe()
  }
}
