function doPost(e) {
  try {
    var payload = JSON.parse((e && e.postData && e.postData.contents) || '{}');
    var expected = PropertiesService.getScriptProperties().getProperty('MAIL_WEBHOOK_TOKEN');
    if (!expected || !payload.token || payload.token !== expected) {
      return json_({ sent: false, reason: 'Unauthorized email request.' });
    }

    var o = payload.order || {};
    if (!o.order_id || !o.email || !/^[^\s@]+@[^\s@]+\.[^\s@]+$/.test(o.email)) {
      return json_({ sent: false, reason: 'Order or customer email is missing.' });
    }

    var items = (o.items || []).map(function (item) {
      return '<tr><td style="padding:10px;border-bottom:1px solid #e9e6df">' +
        escapeHtml_(item.name) + ' · ' + escapeHtml_(item.color) + ' / ' + escapeHtml_(item.size) +
        ' × ' + Number(item.quantity) + '</td><td style="padding:10px;border-bottom:1px solid #e9e6df;text-align:right">₹' +
        Number(item.line_total).toLocaleString('en-IN') + '</td></tr>';
    }).join('');

    var subject = 'Order ' + o.order_id + ' is confirmed · Form & Field';
    var plain = 'Hi ' + o.name + ',\n\nYour Form & Field order ' + o.order_id +
      ' is confirmed. Payment: Cash on delivery. Total: ₹' + Number(o.total).toLocaleString('en-IN') +
      '. Requested delivery: ' + (o.requested_delivery_date || o.delivery) + ' · ' + (o.requested_delivery_slot || 'time to be confirmed') +
      '. Estimated delivery window: ' + (o.estimated_delivery_from || o.delivery) + ' to ' + (o.estimated_delivery_to || o.delivery) + '.\n\nThank you for shopping with us.';
    var html = '<div style="font-family:Arial,sans-serif;max-width:620px;margin:auto;color:#30362d">' +
      '<div style="background:#394637;padding:24px;color:#f8f7f1;font-size:18px;letter-spacing:3px">FORM &amp; FIELD</div>' +
      '<div style="padding:28px"><p>Hi ' + escapeHtml_(o.name) + ',</p><h2 style="font-weight:500">Your order is confirmed.</h2>' +
      '<p>Order <b>' + escapeHtml_(o.order_id) + '</b> · Cash on delivery</p>' +
      '<table style="width:100%;border-collapse:collapse">' + items +
      '<tr><td style="padding:10px">Shipping</td><td style="padding:10px;text-align:right">' +
      (Number(o.shipping) ? '₹' + Number(o.shipping).toLocaleString('en-IN') : 'Free') + '</td></tr>' +
      '<tr><td style="padding:10px;font-weight:bold">Total due on delivery</td><td style="padding:10px;text-align:right;font-weight:bold">₹' +
      Number(o.total).toLocaleString('en-IN') + '</td></tr></table>' +
      '<p>Requested delivery: <b>' + escapeHtml_(o.requested_delivery_date || o.delivery) + ' · ' + escapeHtml_(o.requested_delivery_slot || 'time to be confirmed') + '</b></p>' +
      '<p>Estimated delivery window: ' + escapeHtml_(o.estimated_delivery_from || o.delivery) + ' to ' + escapeHtml_(o.estimated_delivery_to || o.delivery) + '</p>' +
      '<p style="color:#73786f">Deliver to: ' + escapeHtml_(o.address) + '</p>' +
      '<p>Thanks for making room for something good.<br/>Form &amp; Field</p></div></div>';

    GmailApp.sendEmail(o.email, subject, plain, {
      name: 'Form & Field',
      htmlBody: html
    });
    return json_({ sent: true });
  } catch (err) {
    console.error(err);
    return json_({ sent: false, reason: 'Email could not be sent.' });
  }
}

function json_(value) {
  return ContentService.createTextOutput(JSON.stringify(value))
    .setMimeType(ContentService.MimeType.JSON);
}

function escapeHtml_(value) {
  return String(value == null ? '' : value).replace(/[&<>"']/g, function (char) {
    return { '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;' }[char];
  });
}
