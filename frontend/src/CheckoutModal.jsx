import React, { useRef, useState } from 'react';
import { ArrowRight, Check, X } from 'lucide-react';

const API='/api';
const money=n=>`₹${Number(n).toLocaleString('en-IN')}`;

export default function CheckoutModal({cart,total,onClose,onOrderPlaced,customer}) {
  const [busy,setBusy]=useState(false);
  const [error,setError]=useState('');
  const [result,setResult]=useState(null);
  const key=useRef(crypto.randomUUID());
  const shipping=total>=2500?0:99;
  const payable=total+shipping;
  const dateValue=days=>{const d=new Date();d.setDate(d.getDate()+days);return `${d.getFullYear()}-${String(d.getMonth()+1).padStart(2,'0')}-${String(d.getDate()).padStart(2,'0')}`};
  const minDelivery=dateValue(4), maxDelivery=dateValue(7);

  async function submit(e) {
    e.preventDefault(); setBusy(true); setError('');
    const fd=new FormData(e.currentTarget), grouped={};
    for(const p of cart){
      const itemKey=`${p.id}|${p.size}`;
      if(!grouped[itemKey])grouped[itemKey]={product_id:p.id,size:p.size,quantity:0};
      grouped[itemKey].quantity++;
    }
    const payload={name:fd.get('name'),email:fd.get('email'),phone:fd.get('phone'),address:fd.get('address'),city:fd.get('city'),state:fd.get('state'),pincode:fd.get('pincode'),requested_delivery_date:fd.get('requested_delivery_date'),requested_delivery_slot:fd.get('requested_delivery_slot'),items:Object.values(grouped),idempotency_key:key.current};
    try {
      const response=await fetch(`${API}/checkout`,{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify(payload)});
      const data=await response.json();
      if(!response.ok)throw Error(data.detail||'We could not place your order. Please check your details.');
      setResult(data); if(!data.duplicate)onOrderPlaced();
    } catch(err) { setError(err.message||'Checkout could not connect. Please try again.'); }
    finally { setBusy(false); }
  }

  const emailSent=result?.notifications?.email?.sent;
  const whatsappSent=result?.notifications?.whatsapp?.sent;
  return <div className="overlay checkout-overlay"><section className="checkout-modal">
    <button className="close-button" onClick={onClose} aria-label="Close"><X/></button>
    {result ? <div className="checkout-success">
      <div className="success-seal"><Check size={26}/></div><div className="section-eyebrow">ORDER CONFIRMED · CASH ON DELIVERY</div>
      <h2>Thank you, your order is in.</h2><p>We’ve saved order <b>{result.order.id}</b> for {money(result.order.amount)}. Requested delivery: {result.order.requested_delivery_date} · {result.order.requested_delivery_slot}.</p>
      <div className="notice-status"><div><span>{emailSent?'✓':'○'}</span><div><b>Order email</b><small>{emailSent?'Confirmation sent to your inbox':result.notifications?.email?.reason||'Email service needs setup'}</small></div></div><div><span>{whatsappSent?'✓':'○'}</span><div><b>WhatsApp message</b><small>{whatsappSent?'Confirmation sent to your number':result.notifications?.whatsapp?.reason||'WhatsApp needs setup'}</small></div></div></div>
      <button className="dark-cta" onClick={onClose}>Continue shopping <ArrowRight size={16}/></button>
    </div> : <>
      <div className="checkout-heading"><div className="section-eyebrow">A FEW DETAILS AND IT’S YOURS</div><h2>Checkout</h2><p>Pay cash when your pieces arrive. We’ll send an order confirmation to your email and WhatsApp.</p></div>
      <div className="checkout-summary"><span>{cart.length} {cart.length===1?'piece':'pieces'} in your bag</span><b>{money(payable)}</b></div>
      <div className="checkout-prices"><span>Subtotal <b>{money(total)}</b></span><span>Delivery <b>{shipping?money(shipping):'Free'}</b></span><span>Total due on delivery <b>{money(payable)}</b></span></div>
      <form className="checkout-form" onSubmit={submit}>
        <div className="checkout-fields">
          <label>Full name<input name="name" required minLength="2" autoComplete="name" defaultValue={customer?.name||''} readOnly={Boolean(customer)} placeholder="Your name"/></label>
          <label>Email address<input name="email" type="email" required autoComplete="email" defaultValue={customer?.email||''} readOnly={Boolean(customer)} placeholder="you@example.com"/></label>
          <label>Mobile number<input name="phone" type="tel" required minLength="10" autoComplete="tel" defaultValue={customer?.phone||''} readOnly={Boolean(customer)} placeholder="+91 98765 43210"/></label>
          <label>PIN code<input name="pincode" inputMode="numeric" pattern="[1-9][0-9]{5}" required autoComplete="postal-code" placeholder="6-digit PIN code"/></label>
          <label className="field-wide">Street address<input name="address" required minLength="8" autoComplete="street-address" placeholder="House / flat, street, area"/></label>
          <label>City<input name="city" required autoComplete="address-level2" placeholder="City"/></label>
          <label>State<input name="state" required autoComplete="address-level1" placeholder="State"/></label>
          <label>Delivery date<input name="requested_delivery_date" type="date" required min={minDelivery} max={maxDelivery} defaultValue={minDelivery}/></label>
          <label>Delivery time<select name="requested_delivery_slot" required defaultValue=""><option value="" disabled>Select a window</option><option value="09:00-12:00">9 am – 12 pm</option><option value="12:00-15:00">12 pm – 3 pm</option><option value="15:00-18:00">3 pm – 6 pm</option><option value="18:00-21:00">6 pm – 9 pm</option></select></label>
        </div>
        {error&&<div className="checkout-error">{error}</div>}
        <div className="cod-choice"><span className="cod-radio">✓</span><div><b>Cash on delivery</b><small>Pay in cash when your order reaches you.</small></div><b>COD</b></div>
        <button className="dark-cta place-order" disabled={busy||!cart.length}>{busy?'Placing your order…':`Place order · ${money(payable)}`}<ArrowRight size={17}/></button>
        <small className="checkout-terms">By placing your order, you agree to our delivery and returns policy.</small>
      </form>
    </>}
  </section></div>;
}
