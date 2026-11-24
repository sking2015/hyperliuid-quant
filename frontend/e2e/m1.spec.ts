import { expect, test } from '@playwright/test'

test('browse mainnet markets and manage a manual and Top 10 observation pool', async ({ page, request }) => {
  const prefix = `E2E-${crypto.randomUUID().slice(0, 8)}`
  const manualName = prefix + '-manual'
  const dynamicName = prefix + '-top10'
  const errors: string[] = []
  page.on('pageerror', error => errors.push(error.message))
  try {
    await page.goto('/')
    await expect(page.getByText('PAPER MODE')).toBeVisible()
    await page.getByPlaceholder('搜索交易对或资产代码').fill('BTC')
    await expect(page.getByRole('button', { name: 'BTC', exact: true }).first()).toBeVisible()
    await page.getByRole('button', { name: 'BTC', exact: true }).first().click()
    await expect(page.getByRole('dialog')).toBeVisible()
    await expect(page.getByRole('dialog').locator('canvas').first()).toBeVisible()
    await page.getByRole('dialog').getByRole('button', { name: '关闭此对话框' }).click()

    await page.getByRole('button', { name: '观察池', exact: true }).click()
    await page.getByRole('textbox', { name: '名称' }).fill(manualName)
    await page.getByRole('button', { name: '保存', exact: true }).click()
    await expect(page.getByRole('button', { name: manualName + ' · 手动' })).toBeVisible()
    await page.getByRole('button', { name: '市场', exact: true }).click()
    await page.getByPlaceholder('搜索交易对或资产代码').fill('BTC')
    await page.getByText('选择操作目标池', { exact: true }).click()
    await page.getByRole('option', { name: manualName }).click()
    const btc = page.getByRole('row').filter({ has: page.getByRole('button', { name: 'BTC', exact: true }) }).first()
    await btc.getByRole('button', { name: '加入', exact: true }).click()
    await expect(page.getByText('已手动加入观察池')).toBeVisible()

    await page.getByRole('button', { name: '观察池', exact: true }).click()
    await page.getByRole('button', { name: manualName + ' · 手动' }).click()
    await expect(page.getByRole('cell', { name: 'BTC', exact: true })).toBeVisible()
    await page.getByRole('button', { name: '新建观察池', exact: true }).click()
    await page.getByRole('textbox', { name: '名称' }).fill(dynamicName)
    await page.getByRole('combobox', { name: '类型' }).press('ArrowDown')
    await page.getByRole('option', { name: '动态', exact: true }).click()
    await page.getByRole('button', { name: '预览当前配置' }).click()
    await expect(page.getByRole('cell', { name: 'PERP::0', exact: true })).toBeVisible()
    await page.getByRole('button', { name: '保存', exact: true }).click()
    await expect(page.getByRole('button', { name: dynamicName + ' · 动态' })).toBeVisible()
    await page.getByRole('button', { name: '按已保存配置刷新' }).click()
    await expect(page.getByRole('heading', { name: '进出池记录' })).toBeVisible()
    await expect(page.getByText(/规则 v1 · 10 条记录/).first()).toBeVisible()
    await page.getByRole('button', { name: '系统', exact: true }).click()
    await expect(page.getByText('采集心跳 · 正常', { exact: false })).toBeVisible()
    expect(errors).toEqual([])
  } finally {
    const response = await request.get('/api/v1/watchlists', { timeout: 3000 }).catch(() => undefined)
    if (response?.ok()) {
      const pools: { id: string; name: string }[] = await response.json()
      await Promise.all(pools.filter(pool => pool.name.startsWith(prefix)).map(pool =>
        request.delete('/api/v1/watchlists/' + pool.id)))
    }
  }
})

