# This file is part of Maker Keeper Framework.
#
# Copyright (C) 2017-2018 reverendus
#
# This program is free software: you can redistribute it and/or modify
# it under the terms of the GNU Affero General Public License as published by
# the Free Software Foundation, either version 3 of the License, or
# (at your option) any later version.
#
# This program is distributed in the hope that it will be useful,
# but WITHOUT ANY WARRANTY; without even the implied warranty of
# MERCHANTABILITY or FITNESS FOR A PARTICULAR PURPOSE.  See the
# GNU Affero General Public License for more details.
#
# You should have received a copy of the GNU Affero General Public License
# along with this program.  If not, see <http://www.gnu.org/licenses/>.

import logging
import threading

import time


class OrderBook:
    """Represents the current snapshot of the order book.

    Attributes:
        orders: Current list of active keeper orders. This list is already amended with
            recently placed orders, also recently cancelled orders or orders being currently cancelled
            are not present in it.

        balances: Current balances state. This field only has value when balance retrieval function
            has been configured by invoking  OrderBookManager.get_balances_with()`. Otherwise it's always
            None. Currently, the balances state is not updated with order placement and cancellation
            (unlike `orders`) so it may (and will) happen that `orders` and `balances` will get out of sync.
            There are no serious consequences of it, keeper may be trying to place orders with money
            it doesn't have yet, which will of course fail, but the state will be rectified the
            next time a successful orders/balances sync takes place.

        orders_being_placed: `True` if at least one order is currently being placed. `False` otherwise.
            Orders which are currently being placed are not included in `orders`. They will only get
            included there the moment order placement succeeds.

        orders_being_cancelled: `True` if at least one orders is currently being cancelled. `False` otherwise.
            Orders which are currently being cancelled are immediately removed from `orders`. Having said that,
            they will 'reappear' there again if the cancellation fails. It's the keepers responsibility
            to notice them and try to cancel them again.
    """
    def __init__(self,
                 orders,
                 balances,
                 orders_being_placed: bool,
                 orders_being_cancelled: bool):
        assert(isinstance(orders_being_placed, bool))
        assert(isinstance(orders_being_cancelled, bool))

        self.orders = orders
        self.balances = balances
        self.orders_being_placed = orders_being_placed
        self.orders_being_cancelled = orders_being_cancelled


class OrderBookManager:
    """Order book manager allows keeper to track state of the order book without constantly querying it.

    Some exchange APIs are not very good in responding to API requests. If a place order API call
    periodically fails it's not such a big deal for the keeper, but if a `get_orders()` call starts
    to fail for a few minutes this can have tremendous consequences if the keeper relies on that
    call directly. For example the keeper may not be able to cancel orders as the price moves.

    Order book manager allows to decouple the keeper from directly depending on the `get_orders()` call
    in order to be aware of the current state of its orders. It queries the order book periodically
    in background, allowing the keeper to get the latest snapshot of it. In addition to that, for orders
    placed or cancelled via the order book manager it can update this internal snapshot by 'forgetting'
    the cancelled ones and 'amending' the snapshot with the newly placed ones. See the `OrderBook` class.

    This way, as long as the `place_order()` call is able to return the id of the newly placed order,
    the keeper can cancel these orders even if no `get_orders()` call has been successful since then.

    Order book manager can also optionally query the balances and include them in the snapshot,
    along querying the order book.

    Attributes:
        refresh_frequency: Frequency (in seconds) of how often background order book (and balances)
            refresh takes place.
    """

    logger = logging.getLogger()

    def __init__(self, refresh_frequency: int):
        assert(isinstance(refresh_frequency, int))

        self.refresh_frequency = refresh_frequency
        self.get_orders_function = None
        self.get_balances_function = None

        self._lock = threading.Lock()
        self._state = None
        self._refresh_count = 0
        self._currently_placing_orders = 0
        self._orders_placed = list()
        self._order_ids_cancelling = set()
        self._order_ids_cancelled = set()

    def get_orders_with(self, get_orders_function):
        """Configures the function used to fetch active keeper orders.

        Args:
            get_orders_function: The function which will be periodically called by the order book manager
                in order to get active orders. It has to be configured before `start()` gets called.
        """
        assert(callable(get_orders_function))

        self.get_orders_function = get_orders_function

    def get_balances_with(self, get_balances_function):
        """Configures the (optional) function used to fetch current keeper balances.

        Args:
            get_balances_function: The function which will be periodically called by the order book manager
                in order to get current keeper balances. This is optional, is not configured balances
                will not be fetched.
        """
        assert(callable(get_balances_function))

        self.get_balances_function = get_balances_function

    def start(self):
        """Start the background refresh of active keeper orders."""
        threading.Thread(target=self._thread_refresh_order_book, daemon=True).start()

    def get_order_book(self) -> OrderBook:
        """Returns the current snapshot of the active keeper orders and balances.

        Place see the `OrderBook` class for detailed description of all returned fields.

        Returns:
            An `OrderBook` class instance.
        """
        while self._state is None:
            self.logger.info("Waiting for the order book to become available...")
            time.sleep(0.5)

        with self._lock:
            self.logger.debug(f"Getting the order book")
            self.logger.debug(f"Orders retrieved last time: {[order.order_id for order in self._state['orders']]}")
            self.logger.debug(f"Orders placed since then: {[order.order_id for order in self._orders_placed]}")
            self.logger.debug(f"Orders cancelled since then: {[order_id for order_id in self._order_ids_cancelled]}")
            self.logger.debug(f"Orders being cancelled: {[order_id for order_id in self._order_ids_cancelling]}")

            # TODO: below we remove orders which are being or have been cancelled, and orders
            # which have been placed, but we to not update the balances accordingly. it will
            # work correctly as long as the market maker keeper has enough balance available.
            # when it will get low on balance, order placement may fail or too tiny replacement
            # orders may get created for a while.

            # Add orders which have been placed.
            orders = list(self._state['orders'])
            for order in self._orders_placed:
                if order.order_id not in list(map(lambda order: order.order_id, orders)):
                    orders.append(order)

            # Remove orders being cancelled and already cancelled.
            orders = list(filter(lambda order: order.order_id not in self._order_ids_cancelling and
                                               order.order_id not in self._order_ids_cancelled, orders))

            self.logger.debug(f"Returned orders: {[order.order_id for order in orders]}")

        return OrderBook(orders=orders,
                         balances=self._state['balances'],
                         orders_being_placed=self._currently_placing_orders > 0,
                         orders_being_cancelled=len(self._order_ids_cancelling) > 0)

    def place_order(self, place_order_function):
        """Places new order. Order placement will happen in a background thread.

        Args:
            place_order_function: Function used to place the order.
        """
        assert(callable(place_order_function))

        with self._lock:
            self._currently_placing_orders += 1

        threading.Thread(target=self._thread_place_order(place_order_function)).start()

    def cancel_order(self, order_id: int, cancel_order_function):
        """Cancels an existing order. Order cancellation will happen in a background thread.

        Args:
            order_id: Identified of the order to cancel. It is only used to hide the order
                from the order book snapshot during the cancellation takes place, and to permanently
                remove it from there if the cancellation is successful.

            cancel_order_function: Function used to cancel the order.
        """
        assert(isinstance(order_id, int))
        assert(callable(cancel_order_function))

        with self._lock:
            self._order_ids_cancelling.add(order_id)

        threading.Thread(target=self._thread_cancel_order(order_id, cancel_order_function)).start()

    def wait_for_order_cancellation(self):
        """Wait until no background order cancellation takes place."""
        while len(self._order_ids_cancelling) > 0:
            time.sleep(0.1)

    def wait_for_order_book_refresh(self):
        """Wait until at least one background order book refresh happens since now."""
        with self._lock:
            old_counter = self._refresh_count

        while True:
            with self._lock:
                new_counter = self._refresh_count

            if new_counter > old_counter:
                break

            time.sleep(0.1)

    def wait_for_stable_order_book(self):
        """Wait until no background order placement nor cancellation takes place."""
        while True:
            order_book = self.get_order_book()
            if not order_book.orders_being_cancelled and not order_book.orders_being_placed:
                break
            time.sleep(0.1)

    def _thread_refresh_order_book(self):
        while True:
            try:
                with self._lock:
                    orders_already_cancelled_before = set(self._order_ids_cancelled)
                    orders_already_placed_before = set(self._orders_placed)

                # get orders, get balances
                orders = self.get_orders_function()
                balances = self.get_balances_function() if self.get_balances_function is not None else None

                with self._lock:
                    self._order_ids_cancelled = self._order_ids_cancelled - orders_already_cancelled_before
                    for order in orders_already_placed_before:
                        self._orders_placed.remove(order)

                    if self._state is None:
                        self.logger.info("Order book became available")

                    self._state = {'orders': orders, 'balances': balances}
                    self._refresh_count += 1

                self.logger.debug(f"Fetched the order book"
                                  f" (orders: {[order.order_id for order in orders]})")
            except Exception as e:
                self.logger.info(f"Failed to fetch the order book ({e})")

            time.sleep(self.refresh_frequency)

    def _thread_place_order(self, place_order_function):
        assert(callable(place_order_function))

        def func():
            try:
                new_order = place_order_function()

                if new_order is not None:
                    with self._lock:
                        self._orders_placed.append(new_order)
            finally:
                with self._lock:
                    self._currently_placing_orders -= 1

        return func

    def _thread_cancel_order(self, order_id: int, cancel_order_function):
        assert(isinstance(order_id, int))
        assert(callable(cancel_order_function))

        def func():
            try:
                if cancel_order_function():
                    with self._lock:
                        self._order_ids_cancelled.add(order_id)
                        self._order_ids_cancelling.remove(order_id)
            finally:
                with self._lock:
                    try:
                        self._order_ids_cancelling.remove(order_id)
                    except KeyError:
                        pass

        return func
