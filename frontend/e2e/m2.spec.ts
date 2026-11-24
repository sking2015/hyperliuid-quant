import { expect, test } from '@playwright/test'

test('configure EMA and open a persisted backtest report and trade chart', async ({ page, request }) => {
  const reportId = process.env.M2_REPORT_ID
  test.skip(!reportId, 'Set M2_REPORT_ID to a completed backtest with trades')
  const response = await request.get('/api/v1/backtests/' + reportId + '/report')
  expect(response.ok()).toBeTruthy()
  const report = await response.json()
  expect(report.trades.length).toBeGreaterThan(0)
  const errors: string[] = []
  page.on('pageerror', error => errors.push(error.message))
  await page.goto('/')
  await page.getByRole('button', { name: '策略', exact: true }).click()
  const fast = page.getByRole('spinbutton', { name: '快 EMA' })
  await expect(fast).toHaveValue('9')
  await fast.fill('8')
  await fast.press('Tab')
  await page.getByRole('button', { name: '使用此配置回测' }).click()
  await expect(page.getByRole('heading', { name: '新建回测' })).toBeVisible()
  await expect(page.getByRole('spinbutton', { name: '快 EMA' })).toHaveValue('8')
  await expect(page.getByRole('button', { name: '检查数据覆盖' })).toBeEnabled()
  const row = page.getByRole('row').filter({ hasText: 'ema_crossover' }).filter({ hasText: '已完成' }).first()
  await row.getByRole('button', { name: '查看', exact: true }).click()
  await expect(page.getByRole('img', { name: '账户权益曲线' }).locator('canvas')).toBeVisible()
  await expect(page.getByText('Profit Factor', { exact: true })).toBeVisible()
  await page.getByRole('row').filter({ has: page.getByRole('button', { name: 'BTC', exact: true }) }).first()
    .getByRole('button', { name: 'BTC', exact: true }).click()
  await expect(page.getByRole('dialog', { name: 'BTC' }).locator('canvas').first()).toBeVisible()
  expect(errors).toEqual([])
})

