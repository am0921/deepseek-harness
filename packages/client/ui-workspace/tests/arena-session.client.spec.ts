import { describe, expect, it, vi } from 'vitest'
import type { SessionId } from '@deepseek-ai/dsh-session/types'
import { followArenaSession } from '../src/client/arena-session.ts'

function fixture() {
  const listeners = new Set<() => void>()
  const id = 'arena-session' as SessionId
  let present = false
  const source = {
    getSnapshot: () => ({ ids: present ? [id] : [] }),
    subscribe: (listener: () => void) => {
      listeners.add(listener)
      return () => { listeners.delete(listener) }
    },
  }
  return {
    id, list: source, listeners,
    arrive: () => {
      present = true
      for (const listener of listeners) listener()
    },
  }
}

describe('Arena Session navigation', () => {
  it('keeps ordinary Dsh navigation unchanged without an Arena query', () => {
    const state = fixture()
    const open = vi.fn()
    const stop = followArenaSession('', state.list, open)
    state.arrive()
    stop()
    expect(open).not.toHaveBeenCalled()
    expect(state.listeners.size).toBe(0)
  })

  it('opens only the existing requested Session and selects it once', () => {
    const state = fixture()
    const open = vi.fn()
    const stop = followArenaSession('?arenaSession=arena-session', state.list, open)
    expect(open).not.toHaveBeenCalled()
    state.arrive()
    state.arrive()
    expect(open).toHaveBeenCalledExactlyOnceWith(state.id)
    stop()
    expect(state.listeners.size).toBe(0)
  })

  it('selects an already listed Session on initial attachment', () => {
    const state = fixture()
    state.arrive()
    const open = vi.fn()
    const stop = followArenaSession('?arenaSession=arena-session', state.list, open)
    expect(open).toHaveBeenCalledExactlyOnceWith(state.id)
    stop()
  })

  it('does not navigate after the owning fiber is disposed', () => {
    const state = fixture()
    const open = vi.fn()
    const stop = followArenaSession('?arenaSession=arena-session', state.list, open)
    stop()
    state.arrive()
    expect(open).not.toHaveBeenCalled()
  })
})
