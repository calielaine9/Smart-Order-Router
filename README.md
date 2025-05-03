# Static SOR Back-Test

## 1. Code structure
- **backtest.py**  
  - **load_snapshots**: read `l1_day.csv`, dedupe per (`ts_event`,`publisher_id`), yield time-ordered Level-1 snapshots  
  - **allocate**: exhaustive 100-share–chunk search for the cost-minimizing split across venues, exactly per `allocator_pseudocode.txt`  
  - **compute_cost**: implements Cont & Kukanov’s static cost model (paid ask+fee, earned rebate, λ_under/λ_over penalties, θ_queue risk)  
  - **backtest_static**: for each snapshot, re-run `allocate` on the **remaining** shares, execute up to displayed size, roll forward unfilled quantity  
  - **baselines**:  
    1. **Best-ask**: always cross the single cheapest ask  
    2. **TWAP**: divide 5 000 shares equally across each 60-second bucket, always hit best ask  
    3. **VWAP**: at each tick, submit pro-rata to ask sizes based on original 5 000-share weights  
  - **grid search**: brute-force over λ_over, λ_under, θ_queue in coarse→fine steps  
  - **output**: single JSON to stdout with best params, total cash, avg price, baseline stats, bps savings  
  - **cumulative-cost plot**: saved as `results.png`

## 2. Parameter search rationale
We must balance three competing objectives—avoiding under-fills, preventing over-fills, and limiting queue-risk—so we search:

1. **λ_under** (per-share under-fill penalty)  
   - **Why**: reflects urgency to finish all 5 000 shares  
   - **Too low** ⇒ heavy reliance on cheap limit orders but risk missing target  
   - **Too high** ⇒ effectively forces market orders  
   - **Grid**: start at roughly one penny per share (0.01 $) up to 0.08 $, with 8 steps  

2. **λ_over** (per-share over-fill penalty)  
   - **Why**: discourages sending too many limit orders that might all fill  
   - **Nonzero** ensures we don’t “overbook” excessively  
   - **Typically** should be smaller than λ_under  
   - **Grid**: from 0.001 $ up to 0.02 $, with 6 steps  

3. **θ_queue** (queue-position risk coefficient)  
   - **Why**: penalizes the cost of being stuck at the back of deep queues  
   - **Captures** expected slippage from waiting  
   - **Small changes** can shift allocation toward faster-filling venues  
   - **Grid**: 0 to 0.005 $, with 50 fine steps  

We cache backtest results (`lru_cache`) and parallelize grid evaluations (`concurrent.futures`) to keep total runtime under two minutes.

## 3. Suggested improvements
1. **Overbooking simulation**  
   - In reality, traders often place slightly more shares than needed across venues, then cancel unfilled orders (“phantom liquidity”).  
   - _Extension_: allow the allocator to overshoot S by a configurable fraction, then subtract filled shares and revoke the remainder, to better match empirical fill correlations.  
2. **Empirical slippage model**  
   - Each execution tranche can move the mid-price slightly.  
   - _Extension_: add a small linear price impact term proportional to executed volume per snapshot.  
3. **Adaptive urgency**  
   - Market conditions vary: if recent spread volatility is high, under-fill penalty should increase.  
   - _Extension_: tie λ_under to a rolling estimate of mid-quote variance, so the router self-adjusts in fast markets.  
