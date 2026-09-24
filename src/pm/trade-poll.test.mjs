import assert from 'node:assert/strict';
import {dueTradeMarkets} from './trade-poll.mjs';
const activity = new Map(Array.from({length:60}, (_,i)=>[`m${i}`,i]));
const seen = new Set(), polled = new Map();
// Each round takes longer than the refresh period: still visit every market.
for(let round=0;round<3;round++) {
  const now=1000000+round*300000;
  for(const id of dueTradeMarkets(activity,polled,{now})) {seen.add(id);polled.set(id,now);}
}
assert.equal(seen.size,60);
activity.set('new',999);
assert.equal(dueTradeMarkets(activity,polled,{now:2000000})[0],'new');
assert.deepEqual(dueTradeMarkets(new Map([['a',1]]),new Map([['a',1000]]),{now:2000,every:10000}),[]);
console.log('trade polling fairness passed');
