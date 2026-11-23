import { api, chip, escapeHtml } from '../api.js';

export async function renderQc(el) {
  const [findings, batches, cleanses] = await Promise.all([
    api('/api/findings'),
    api('/api/batches'),
    api('/api/cleanses'),
  ]);
  const summary = findings.summary || {};
  const badge = cleanses.summary || {};
  const pending = cleanses.pending || [];
  const vouchers = cleanses.items || [];
  const openBatches = (batches.items || []).filter((b) => b.status !== '已关闭');
  const consistent = badge.badge_consistent !== false;

  el.innerHTML = `
    <div class="card">
      <h2>字段质控发现</h2>
      <p class="muted">对缺口质量行扫描生成发现；缺字段/编码空/重复键会带严重度。关闭发现不自动改质量，补采成功后会联动关闭。</p>
      <div class="grid">
        <div class="stat"><div class="n">${summary.open || 0}</div><div class="l">开放发现</div></div>
        <div class="stat"><div class="n">${summary.closed || 0}</div><div class="l">已关闭</div></div>
        <div class="stat"><div class="n">${(summary.by_severity && summary.by_severity['严重']) || 0}</div><div class="l">严重</div></div>
        <div class="stat"><div class="n">${(summary.by_severity && summary.by_severity['一般']) || 0}</div><div class="l">一般</div></div>
      </div>
      <div class="row">
        <select id="scanBatch">
          ${openBatches.map((b) => `<option value="${b.id}">${escapeHtml(b.batch_code + ' · ' + b.site_name)}</option>`).join('')}
        </select>
        <button id="scanOne">扫描选中批次</button>
        <button id="scanAll" class="warn">扫描全部开放批次</button>
        <select id="statusFilter">
          <option value="开放">仅开放</option>
          <option value="">全部状态</option>
          <option value="已关闭">仅已关闭</option>
        </select>
        <select id="sevFilter">
          <option value="">全部严重度</option>
          <option>提示</option>
          <option>一般</option>
          <option>严重</option>
        </select>
        <button id="reload" class="ghost">刷新</button>
      </div>
      <div id="msg" class="muted"></div>
    </div>
    <div class="card">
      <div class="row" style="justify-content:space-between">
        <h2 style="margin:0">编码清洗</h2>
        <span id="cleanseBadge" class="chip ${consistent ? 'ok' : 'bad'}">
          清洗徽标：封档凭证 ${badge.sealed_vouchers || 0} · 空码条数 ${badge.empty_code_total || 0}
          ${consistent ? '· 口径对齐' : '· 徽标拧花，空码入库已拦'}
        </span>
      </div>
      <p class="muted">编码清洗走甲侧封档凭证：一行一张凭证、当场钉死唯一键、挂 SHA-256 指纹，凭证只读拒绝回刷；无凭证或指纹不符的编码空行一律禁止入库。口径：${escapeHtml(badge.rule || '-')}</p>
      <div class="grid">
        <div class="stat"><div class="n">${badge.empty_code_total || 0}</div><div class="l">业务库空码条数</div></div>
        <div class="stat"><div class="n">${badge.pending_empty_code || 0}</div><div class="l">待清洗空码（清洗队列）</div></div>
        <div class="stat"><div class="n">${badge.sealed_vouchers || 0}</div><div class="l">封档凭证（清洗落点）</div></div>
        <div class="stat"><div class="n">${badge.invalid_vouchers || 0}</div><div class="l">指纹失效凭证</div></div>
      </div>
      <div id="cleanseMsg" class="${consistent ? 'muted' : 'err'}">
        ${consistent
          ? '徽标数与业务库空码条数对齐：空码行均在清洗队列，封档行均已转合格入库。'
          : '徽标与空码条数拧花：存在未清洗空码或指纹失效凭证，入库闸门已拦截相关行。'}
      </div>
    </div>
    <div class="card">
      <h2>编码清洗队列（待清洗空码行）</h2>
      <table>
        <thead>
          <tr><th>行ID</th><th>批次/中心</th><th>受试者</th><th>原访视编码</th><th>原载荷摘要</th><th>清洗提交</th></tr>
        </thead>
        <tbody id="pendingBody">
          ${pending.map(pendingRow).join('') || '<tr><td colspan="6" class="muted">清洗队列已空，无待清洗空码</td></tr>'}
        </tbody>
      </table>
    </div>
    <div class="card">
      <h2>编码清洗凭证（封档落点 · 只读）</h2>
      <table>
        <thead>
          <tr><th>凭证号</th><th>行</th><th>批次</th><th>受试者/清洗后编码</th><th>指纹核验</th><th>封档时间</th><th>操作</th></tr>
        </thead>
        <tbody id="voucherBody">
          ${vouchers.map(voucherRow).join('') || '<tr><td colspan="7" class="muted">尚无封档凭证</td></tr>'}
        </tbody>
      </table>
      <div id="voucherDetail" class="detail-box" style="margin-top:12px">点「详情/核验」查看凭证串：旧载荷→新载荷→旧指纹→封档指纹。</div>
    </div>
    <div class="card">
      <h2>发现列表</h2>
      <table>
        <thead>
          <tr>
            <th>ID</th><th>批次/中心</th><th>受试者访视</th><th>字段</th>
            <th>严重度</th><th>状态</th><th>说明</th><th>操作</th>
          </tr>
        </thead>
        <tbody id="body">
          ${(findings.items || []).map(rowHtml).join('') || '<tr><td colspan="8" class="muted">暂无发现</td></tr>'}
        </tbody>
      </table>
      <div class="card" style="margin-top:12px;box-shadow:none">
        <h2 style="font-size:15px">按字段汇总（开放）</h2>
        <div class="row">
          ${(summary.by_field || []).map((f) => `
            <span class="chip warn">${escapeHtml(f.field_name)} · ${f.c}</span>
          `).join('') || '<span class="muted">无</span>'}
        </div>
      </div>
    </div>`;

  function pendingRow(r) {
    return `
      <tr data-row="${r.id}">
        <td>${r.id}</td>
        <td>${escapeHtml(r.batch_code)}<div class="muted">${escapeHtml(r.site_name || '')}</div></td>
        <td>${escapeHtml(r.subject_key)}</td>
        <td>${chip(r.quality)} ${escapeHtml(r.visit_code || '（空）')}</td>
        <td class="muted">${escapeHtml((r.payload || '').slice(0, 60) || '（空载荷）')}</td>
        <td>
          <div class="row">
            <input data-code="${r.id}" placeholder="清洗后访视编码" value="${escapeHtml(r.visit_code || '')}" style="width:130px" />
            <input data-payload="${r.id}" placeholder='清洗后载荷 JSON' style="min-width:280px"
              value='{"visit_date":"2026-11-23","investigator":"清洗医生"}' />
            <button data-cleanse="${r.id}" class="warn">提交清洗封档</button>
          </div>
        </td>
      </tr>`;
  }

  function voucherRow(v) {
    const ok = v.verify && v.verify.verified;
    return `
      <tr>
        <td><code>${escapeHtml(v.voucher_code)}</code></td>
        <td>#${v.row_id}</td>
        <td>${escapeHtml(v.batch_code)}<div class="muted">${escapeHtml(v.site_name || '')}</div></td>
        <td>${escapeHtml(v.subject_key)} / <strong>${escapeHtml(v.visit_code)}</strong><div>${chip(v.row_quality)} ${chip(v.row_status)}</div></td>
        <td><span class="chip ${ok ? 'ok' : 'bad'}">${ok ? '指纹一致' : '指纹不符'}</span></td>
        <td class="muted">${escapeHtml(v.created_at || '')}</td>
        <td><button data-voucher="${v.id}" class="ghost">详情/核验</button></td>
      </tr>`;
  }

  function rowHtml(f) {
    return `
      <tr>
        <td>${f.id}</td>
        <td>${escapeHtml(f.batch_code)}<div class="muted">${escapeHtml(f.site_name || '')}</div></td>
        <td>${escapeHtml(f.subject_key)} / ${escapeHtml(f.visit_code)}<div>${chip(f.quality)}</div></td>
        <td>${escapeHtml(f.field_name)}</td>
        <td>${chip(f.severity)}</td>
        <td>${chip(f.status)}</td>
        <td class="muted">${escapeHtml(f.message || '')}</td>
        <td class="row">
          ${f.status === '开放'
            ? `<button data-close="${f.id}">关闭</button>`
            : `<button data-reopen="${f.id}" class="ghost">重开</button>`}
          <button data-view="${f.id}" class="ghost">详情</button>
        </td>
      </tr>`;
  }

  const msg = el.querySelector('#msg');
  const body = el.querySelector('#body');
  const cleanseMsg = el.querySelector('#cleanseMsg');

  function bindCleanses() {
    el.querySelectorAll('[data-cleanse]').forEach((btn) => {
      btn.onclick = async () => {
        const rowId = Number(btn.dataset.cleanse);
        const visitCode = el.querySelector(`input[data-code="${rowId}"]`).value;
        const payload = el.querySelector(`input[data-payload="${rowId}"]`).value;
        try {
          const v = await api('/api/cleanses', {
            method: 'POST',
            body: JSON.stringify({ row_id: rowId, visit_code: visitCode, payload }),
          });
          cleanseMsg.className = 'muted';
          cleanseMsg.textContent = `清洗已封档：凭证 ${v.voucher_code}，指纹核验 ${v.verify.verified ? '一致' : '不符'}，访视行已转合格入库。`;
          await renderQc(el);
        } catch (e) {
          cleanseMsg.className = 'err';
          cleanseMsg.textContent = '清洗被拦：' + e.message;
        }
      };
    });
    el.querySelectorAll('[data-voucher]').forEach((btn) => {
      btn.onclick = async () => {
        try {
          const v = await api(`/api/cleanses/${btn.dataset.voucher}`);
          const verify = await api(`/api/cleanses/${btn.dataset.voucher}/verify`, { method: 'POST', body: '{}' });
          el.querySelector('#voucherDetail').textContent = JSON.stringify({
            凭证号: v.voucher_code,
            状态: v.status,
            行ID: v.row_id,
            受试者: v.subject_key,
            清洗后编码: v.visit_code,
            原载荷: v.old_payload || '（空）',
            清洗后载荷: v.new_payload,
            旧指纹: v.old_fingerprint,
            封档指纹: v.fingerprint,
            令牌: v.token,
            核验: verify,
          }, null, 2);
        } catch (e) {
          el.querySelector('#voucherDetail').textContent = '错误：' + e.message;
        }
      };
    });
  }

  async function reload() {
    const status = el.querySelector('#statusFilter').value;
    const sev = el.querySelector('#sevFilter').value;
    const qs = new URLSearchParams();
    if (status) qs.set('status', status);
    if (sev) qs.set('severity', sev);
    const data = await api('/api/findings?' + qs.toString());
    body.innerHTML = (data.items || []).map(rowHtml).join('') || '<tr><td colspan="8" class="muted">暂无发现</td></tr>';
    bindRows();
  }

  function bindRows() {
    el.querySelectorAll('[data-close]').forEach((btn) => {
      btn.onclick = async () => {
        try {
          await api(`/api/findings/${btn.dataset.close}/close`, { method: 'POST', body: '{}' });
          await reload();
        } catch (e) { msg.textContent = e.message; msg.className = 'err'; }
      };
    });
    el.querySelectorAll('[data-reopen]').forEach((btn) => {
      btn.onclick = async () => {
        try {
          await api(`/api/findings/${btn.dataset.reopen}/reopen`, { method: 'POST', body: '{}' });
          await reload();
        } catch (e) { msg.textContent = e.message; msg.className = 'err'; }
      };
    });
    el.querySelectorAll('[data-view]').forEach((btn) => {
      btn.onclick = async () => {
        try {
          const f = await api(`/api/findings/${btn.dataset.view}`);
          msg.className = 'muted';
          msg.textContent = JSON.stringify({
            发现: f.id,
            字段: f.field_name,
            严重度: f.severity,
            状态: f.status,
            受试者: f.subject_key,
            访视: f.visit_code,
            载荷: f.payload,
            说明: f.message,
          }, null, 2);
        } catch (e) { msg.textContent = e.message; msg.className = 'err'; }
      };
    });
  }

  el.querySelector('#scanOne').onclick = async () => {
    try {
      const batchId = Number(el.querySelector('#scanBatch').value);
      const r = await api('/api/findings', {
        method: 'POST',
        body: JSON.stringify({ batch_id: batchId }),
      });
      msg.className = 'muted';
      msg.textContent = `扫描完成，新建 ${r.created} 条发现，当前开放合计 ${r.open_total}`;
      await reload();
    } catch (e) { msg.textContent = e.message; msg.className = 'err'; }
  };
  el.querySelector('#scanAll').onclick = async () => {
    try {
      const r = await api('/api/findings', {
        method: 'POST',
        body: JSON.stringify({ scan_all: true }),
      });
      msg.className = 'muted';
      msg.textContent = `全量扫描新建 ${r.created} 条`;
      await reload();
    } catch (e) { msg.textContent = e.message; msg.className = 'err'; }
  };
  el.querySelector('#reload').onclick = () => renderQc(el);
  el.querySelector('#statusFilter').onchange = () => reload();
  el.querySelector('#sevFilter').onchange = () => reload();
  bindRows();
  bindCleanses();
}
