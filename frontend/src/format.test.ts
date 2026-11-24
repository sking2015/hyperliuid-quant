import { describe, expect, it } from 'vitest'
import { dateTime, numeric, percent } from './format'

describe('market display', () => {
  it('preserves explicit missing values and zero', () => {
    expect(numeric(null)).toBe('N/A')
    expect(numeric(0)).toBe('0')
    expect(percent(null)).toBe('N/A')
    expect(percent(-0.005)).toBe('-0.5%')
  })
  it('displays UTC explicitly by formatter configuration', () => {
    expect(dateTime('2026-11-24T08:30:00+08:00', true)).toContain('00:30:00')
  })
})

