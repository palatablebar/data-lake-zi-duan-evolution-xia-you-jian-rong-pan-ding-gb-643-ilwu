/* 影响凭证页动作：形成（幂等）、发布、发布失败演练；失败信息必须落页。 */
async function evidenceRequest(url, body) {
  const response = await fetch(url, {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify(body || {}),
  });
  const payload = await response.json().catch(() => null);
  if (!response.ok) {
    const message = (payload && (payload.message || payload.error)) || `HTTP ${response.status}`;
    throw new Error(message);
  }
  return payload;
}

function evidenceFlash(target, message, bad) {
  if (!target) return;
  target.textContent = message;
  target.classList.toggle('is-bad', Boolean(bad));
  target.classList.toggle('is-ok', !bad);
}

document.addEventListener('click', async (event) => {
  const button = event.target.closest('[data-evidence-action]');
  if (!button) return;
  const action = button.dataset.evidenceAction;
  const target = document.querySelector('[data-evidence-flash]');
  button.disabled = true;
  evidenceFlash(target, '…', false);
  try {
    if (action === 'prepare') {
      const result = await evidenceRequest('/api/evidence/prepare', {});
      evidenceFlash(
        target,
        `凭证 ${result.version_id}：确认 ${result.confirmed} / 待补证 ${result.pending} / 不受影响 ${result.unaffected}`
        + (result.reused ? '（命中已有凭证，未新增）' : '（新形成）'),
        false,
      );
    } else if (action === 'publish') {
      const result = await evidenceRequest('/api/evidence/publish', {});
      evidenceFlash(target, `已发布 ${result.version_id}，旧版 ${result.previous_version || '无'} 已留存`, false);
    } else if (action === 'publish-fail') {
      try {
        await evidenceRequest('/api/evidence/publish', { fail_after: true });
        evidenceFlash(target, '演练未按预期失败', true);
      } catch (error) {
        evidenceFlash(target, `${error.message}；刷新可验证旧版仍为 published`, true);
      }
    }
    setTimeout(() => window.location.reload(), 900);
  } catch (error) {
    evidenceFlash(target, error.message, true);
    button.disabled = false;
  }
});
