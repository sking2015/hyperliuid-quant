import { expect, test } from '@playwright/test'

test('inspect an S01 event, its forward labels and raw market replay', async ({ page, request }) => {
  const source = process.env.RESEARCH_TEST_API
  const injected = Boolean(source)
  if (source) {
    await page.route('**/api/v1/research/**', async route => {
      const url = new URL(route.request().url())
      const response = await route.fetch({ url: source + url.pathname + url.search })
      await route.fulfill({ response })
    })
  }
  const prefix = source ? source + '/api/v1/research' : '/api/v1/research'
  const runs = await (await request.get(prefix + '/runs?results=true')).json() as { id: string; status: string; stats: { confirmed?: number } }[]
  const run = runs.find(row => row.status === 'COMPLETE' && (row.stats.confirmed ?? 0) > 0)
  test.skip(!run && !injected, 'No completed research report with confirmed events is available')
  expect(run).toBeTruthy()
  const issues: string[] = []
  page.on('pageerror', error => issues.push(error.message))
  await page.goto('/')
  await page.getByRole('button',{name:'Research Lab',exact:true}).click()
  await expect(page.getByRole('heading',{name:'采集质量'})).toBeVisible()
  await page.getByRole('combobox',{name:'研究任务'}).press('ArrowDown')
  const select = page.getByRole('option').nth(runs.findIndex(row => row.id === run?.id))
  await select.click()
  await expect(page.getByRole('img',{name:'各消融组未来收益曲线'}).locator('canvas').first()).toBeVisible()
  const row = page.getByRole('row').filter({ has: page.getByRole('cell',{name:'确认',exact:true}) }).first()
  await row.getByRole('button',{name:'详情',exact:true}).click()
  const dialog = page.getByRole('dialog')
  await expect(dialog.getByText(/参考中间价/)).toBeVisible()
  await expect(dialog.getByText(/原始可用时间行情/)).toBeVisible()
  await expect(dialog.getByRole('cell',{name:'方向收益',exact:true})).toBeVisible()
  expect(issues).toEqual([])
})

test('automatic replay appears in results only after completion', async ({ page }) => {
  const run = {
    id: 'automatic-replay', mode: 'REPLAY', status: 'QUEUED', asset_ids: ['PERP::0'],
    start_ms: Date.UTC(2026, 0, 1), end_ms: Date.UTC(2026, 0, 1, 1), stats: {},
  }
  let finished = false
  let draft: unknown
  await page.route('**/api/v1/research/**', route => {
    const url = new URL(route.request().url())
    switch (url.pathname.split('/').at(-1)) {
      case 'detectors': return route.fulfill({ json: [{ defaults: {} }] })
      case 'quality': return route.fulfill({ json: { collector: null, streams: [], assets: [{ instrument_id: 'PERP::0', symbol: 'BTC' }] } })
      case 'configs': return route.fulfill({ json: { hash: 'configured' } })
      case 'experiments':
        draft = route.request().postDataJSON()
        return route.fulfill({ json: run })
      case 'automatic-replay': return route.fulfill({ json: { ...run, status: finished ? 'COMPLETE' : 'QUEUED' } })
      case 'runs':
        expect(url.searchParams.get('results')).toBe('true')
        return route.fulfill({ json: finished ? [{ ...run, status: 'COMPLETE' }] : [] })
      default: return route.fulfill({ json: [] })
    }
  })
  await page.goto('/')
  await page.getByRole('button', { name: 'Research Lab', exact: true }).click()
  await page.getByRole('button', { name: '启动研究', exact: true }).click()
  await expect(page.getByText(/^排队 ·/)).toBeVisible()
  expect(draft).toEqual({ mode: 'REPLAY', asset_ids: ['PERP::0'], duration_s: 3600, config_hash: 'configured', auto_range: true })
  const results = page.getByRole('combobox', { name: '研究任务' })
  await results.press('ArrowDown')
  await expect(page.getByText('暂无有效研究结果')).toBeVisible()
  await results.press('Escape')
  finished = true
  await expect(page.getByText(/^完成 ·/)).toBeVisible({ timeout: 15_000 })
  await results.press('ArrowDown')
  await expect(page.getByRole('option')).toHaveCount(1)
  await expect(page.getByRole('option').first()).toContainText('BTC')
})
