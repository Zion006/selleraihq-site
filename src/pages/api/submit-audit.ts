export const prerender = false;

import type { APIRoute } from 'astro';

export const POST: APIRoute = async ({ request, redirect }) => {
  let name = '', storeName = '';
  try {
    const fd = await request.formData();

    name        = (fd.get('name')        as string) || '';
    const email = (fd.get('email')       as string) || '';
    storeName   = (fd.get('store_name')  as string) || '';
    const market= (fd.get('marketplace') as string) || '';
    const gmv   = (fd.get('gmv')         as string) || '';

    if (!name || !email || !storeName || !market || !gmv) {
      return redirect('/audit?error=fields');
    }

    const reimbFile = fd.get('reimb_file') as File | null;
    if (!reimbFile || reimbFile.size === 0) {
      return redirect('/audit?error=file');
    }

    const body = `New FBA Audit Intake Form Submission

Name:          ${name}
Email:         ${email}
Store:         ${storeName}
Marketplace:   ${market}
Monthly GMV:   ${gmv}
SKU Count:     ${fd.get('sku_count') || 'N/A'}
Selling Model: ${fd.get('model') || 'N/A'}

Has reimbursements since March 2025: ${fd.get('has_reimbursements')}
Sourcing cost uploaded:              ${fd.get('sourcing_uploaded')}
Reimbursements looked low:           ${fd.get('looks_low')}
Has supplier invoices:               ${fd.get('has_invoices')}

Notes:
${fd.get('notes') || 'None'}`;

    const attachments: Array<{filename: string; content: string; content_type: string}> = [];

    const reimbBuf = await reimbFile.arrayBuffer();
    attachments.push({
      filename:     reimbFile.name || 'reimbursements.csv',
      content:      Buffer.from(reimbBuf).toString('base64'),
      content_type: reimbFile.type || 'application/octet-stream',
    });

    const invFiles = fd.getAll('invoice_files') as File[];
    for (const f of invFiles) {
      if (f && f.size > 0) {
        const buf = await f.arrayBuffer();
        attachments.push({
          filename:     f.name || 'invoice.csv',
          content:      Buffer.from(buf).toString('base64'),
          content_type: f.type || 'application/octet-stream',
        });
      }
    }

    const apiKey  = import.meta.env.AGENTMAIL_API_KEY;
    const inboxId = import.meta.env.AGENTMAIL_INBOX_ID || 'zionbot@agentmail.to';

    const res = await fetch(
      `https://api.agentmail.to/v0/inboxes/${encodeURIComponent(inboxId)}/messages/send`,
      {
        method: 'POST',
        headers: {
          'Authorization': `Bearer ${apiKey}`,
          'Content-Type':  'application/json',
        },
        body: JSON.stringify({
          to:          [inboxId],
          subject:     `Audit: ${storeName}`,
          text:        body,
          attachments: attachments,
        }),
      }
    );

    if (!res.ok) {
      console.error('AgentMail error', res.status, await res.text());
    }

  } catch (err) {
    console.error('submit-audit error:', err);
  }

  return redirect(
    `/audit/thank-you?name=${encodeURIComponent(name)}&store=${encodeURIComponent(storeName)}`
  );
};
