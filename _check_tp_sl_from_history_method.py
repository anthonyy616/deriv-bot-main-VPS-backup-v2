    async def _check_tp_sl_from_history(self):
        """
        [FIX] History-based TP/SL detection using MT5 deal history.
        Queries authoritative MT5 records to detect when TP or SL was hit.
        This eliminates race conditions from snapshot-based detection.
        """
        try:
            # Query deals since last check
            from_time = datetime.fromtimestamp(self.last_deal_check_time)
            deals = mt5.history_deals_get(from_time, datetime.now(), symbol=self.symbol)
            
            if not deals:
                # No new deals or query failed
                return
            
            for deal in deals:
                # Check if this was a TP or SL closure
                if deal.reason == mt5.DEAL_REASON_TP:
                    reason = "TP"
                elif deal.reason == mt5.DEAL_REASON_SL:
                    reason = "SL"
                else:
                    continue  # Not a TP/SL close, skip
                
                # Map deal to pair using magic number
                if deal.magic < 50000:
                    continue  # Not our order
                
                pair_idx = deal.magic - 50000
                pair = self.pairs.get(pair_idx)
                
                if not pair:
                    continue  # Pair no longer exists
                
                print(f"[{reason}_HIT] {self.symbol}: Pair {pair_idx} - Position {deal.position_id} closed")
                
                # Log to session
                if self.session_logger:
                    self.session_logger.log_tp_sl(
                        symbol=self.symbol,
                        pair_idx=pair_idx,
                        direction="BUY" if deal.type == mt5.DEAL_TYPE_BUY else "SELL",
                        result="tp" if reason == "TP" else "sl",
                        profit=deal.profit
                    )
                
                # [CRITICAL FIX] Reset trade count to 0
                old_count = pair.trade_count
                pair.trade_count = 0
                print(f"   [RESET] Pair {pair_idx} trade_count reset to 0 (was {old_count})")
                
                # Nuclear reset: Close opposite side if still open
                if deal.type == mt5.DEAL_TYPE_SELL:  # Closed a BUY position
                    pair.buy_filled = False
                    pair.buy_ticket = 0
                    
                    # Close opposite SELL if open
                    if pair.sell_filled and pair.sell_ticket:
                        print(f"   [PAIR RESET] Closing opposite Sell {pair.sell_ticket}...")
                        self._close_position(pair.sell_ticket)
                        pair.sell_filled = False
                        pair.sell_ticket = 0
                
                elif deal.type == mt5.DEAL_TYPE_BUY:  # Closed a SELL position
                    pair.sell_filled = False
                    pair.sell_ticket = 0
                    
                    # Close opposite BUY if open
                    if pair.buy_filled and pair.buy_ticket:
                        print(f"   [PAIR RESET] Closing opposite Buy {pair.buy_ticket}...")
                        self._close_position(pair.buy_ticket)
                        pair.buy_filled = False
                        pair.buy_ticket = 0
                
                # Reset flags
                pair.buy_in_zone = False
                pair.sell_in_zone = False
                pair.first_fill_direction = ""
                
                # Cancel any existing pending orders
                if pair.buy_pending_ticket: self._cancel_order(pair.buy_pending_ticket)
                if pair.sell_pending_ticket: self._cancel_order(pair.sell_pending_ticket)
                
                # SET PERSISTENT FLAGS
                pair.pending_reopen_buy = True
                pair.pending_reopen_sell = True
                
                print(f"   [PAIR RESET] Pair {pair_idx} flagged for Reopen. Waiting for retracement...")
                self.save_state()
                
                pair.sell_pending_ticket = self._place_pending_order(
                    self._get_order_type("sell", pair.sell_price),
                    pair.sell_price, pair_idx
                )
                
                print(f"   [PAIR RESET] Pair {pair_idx} fully reset. Sentries re-armed.")
                self.save_state()
            
            # Update last check time
            self.last_deal_check_time = time.time()
            
        except Exception as e:
            print(f"[ERROR] _check_tp_sl_from_history failed: {e}")
            # Don't crash, just skip this tick