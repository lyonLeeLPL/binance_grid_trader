from decimal import Decimal, ROUND_DOWN
from typing import Union, Optional

from gridtrader.trader.object import OrderData, TickData, TradeData, ContractData
from gridtrader.trader.object import Status
from gridtrader.trader.utility import GridPositionCalculator
from gridtrader.tools.common.contract_handler import ContractHandler
from .template import CtaTemplate
from ..engine import CtaEngine
from ...event import EVENT_TIMER


class FutureGridStrategy(CtaTemplate):
    """
    优化后的币安合约网格策略，支持多头和空头模式。
    多头模式下，下方网格的单格金额是上方网格的1.1倍。
    空头模式下，上方网格的单格金额是下方网格的1.1倍。
    总金额保持不变，且满足最小下单数量。
    """
    author = "51bitquant"

    # 参数
    upper_price = 0.0  # 策略最高价
    bottom_price = 0.0  # 策略最低价
    grid_number = 100  # 网格数量
    order_volume = 0.05  # 每次下单数量
    max_open_orders = 5  # 最大同时挂单数
    order_amount = 5000  # 下单总金额
    start_price = 0.0  # 启动价格
    direction_int = 1  # 方向：1为多头，-1为空头

    # 变量
    avg_price = 0.0  # 持仓均价
    step_price = 0.0  # 网格间距
    trade_times = 0  # 成交次数
    price_volume_dict = {}  # 保存每个价格对应的下单数量
    start_price_triggered = False  # 启动价格是否已触发
    lower_grid_total_volume = 0.0  # 下方网格的总下单量
    upper_grid_total_volume = 0.0  # 上方网格的总下单量

    parameters = ["upper_price", "bottom_price", "grid_number", "order_volume", "max_open_orders", "order_amount",
                  "start_price", "direction_int"]
    variables = ["avg_price", "step_price", "trade_times", "price_volume_dict", "start_price_triggered",
                 "lower_grid_total_volume", "upper_grid_total_volume"]

    def __init__(self, cta_engine: CtaEngine, strategy_name, vt_symbol, setting):
        super().__init__(cta_engine, strategy_name, vt_symbol, setting)
        self.long_orders_dict = {}  # 多单挂单字典
        self.short_orders_dict = {}  # 空单挂单字典
        self.tick: Union[TickData, None] = None
        self.contract_data: Optional[ContractData] = None
        self.pos_calculator = GridPositionCalculator()
        self.timer_count = 0
        self._ContractHandler = None
        self.fake_active_orders_price_list = None

    def calculate_grid_parameters(self):
        """
        根据下单金额或手动设置的价格范围和网格数量计算网格参数。
        确保每个网格的单格数量满足最小下单数量，同时保持总下单金额不变。
        """
        if not self.order_amount:
            return

        ###
        fake_active_orders = self.cta_engine.main_engine.future_gateway.active_orders
        self.fake_active_orders_price_list = [float(order.price) for order in fake_active_orders.values()]

        self._ContractHandler = ContractHandler(self.contract_data.price_tick)

        # 获取最小下单数量
        min_volume = self.contract_data.min_volume

        # 根据 min_volume 确定需要保留的小数位数
        decimal_places = abs(Decimal(str(min_volume)).as_tuple().exponent)
        quantize_str = f"0.{'0' * decimal_places}"  # 例如 min_volume=0.001 -> "0.000"

        # 计算价格范围
        price_range = self.upper_price - self.bottom_price
        if price_range <= 0:
            raise ValueError("价格区间设置错误，上限价格必须大于下限价格。")

        # 初始网格数量
        self.grid_number = max(1, int(price_range / (self.bottom_price * 0.006)))  # 初始网格数量基于0.6%间距

        # 调整网格数量，确保每个网格的币数量满足最小下单数量
        while True:
            # 计算网格间距
            self.step_price = price_range / self.grid_number

            # 计算下方网格和上方网格的数量
            lower_grid_number = self.grid_number // 2
            upper_grid_number = self.grid_number - lower_grid_number

            # 计算下方网格和上方网格的单格金额
            if self.direction_int == 1:  # 多头模式
                upper_grid_amount = self.order_amount / (upper_grid_number * 1.1 + lower_grid_number)
                lower_grid_amount = upper_grid_amount * 1.1
            else:  # 空头模式
                lower_grid_amount = self.order_amount / (lower_grid_number * 1.1 + upper_grid_number)
                upper_grid_amount = lower_grid_amount * 1.1

            # 计算每个网格的币数量
            self.price_volume_dict = {}
            total_amount = 0
            for i in range(self.grid_number):
                price = self.bottom_price + i * self.step_price
                if i < lower_grid_number:
                    volume = lower_grid_amount / price  # 金额转换为币数量
                else:
                    volume = upper_grid_amount / price  # 金额转换为币数量

                # 根据 min_volume 保留小数位数
                volume = float(Decimal(volume).quantize(Decimal(quantize_str), rounding=ROUND_DOWN))

                # 如果币数量小于最小下单数量，减少网格数量并重新计算
                if volume < min_volume:
                    self.grid_number -= 1
                    if self.grid_number < 1:
                        raise ValueError("无法满足最小下单数量要求，请调整参数。")
                    break
                else:
                    price_dc = self._ContractHandler.process_price(price)
                    # 将 Decimal 键转换为 float
                    price_float = float(price_dc)
                    self.price_volume_dict[price_float] = volume
                    total_amount += volume * price
            else:
                # 所有网格的币数量都满足最小下单数量要求，退出循环
                break

        # 检查总金额是否超过设定值
        if total_amount > self.order_amount:
            scale_factor = self.order_amount / total_amount
            for price in self.price_volume_dict:
                self.price_volume_dict[price] = float(
                    Decimal(self.price_volume_dict[price] * scale_factor).quantize(Decimal(quantize_str),
                                                                                   rounding=ROUND_DOWN))

        self.write_log(
            f"Calculated Parameters: Upper Price: {self.upper_price}, Bottom Price: {self.bottom_price}, "
            f"Grid Number: {self.grid_number}, Step Price: {self.step_price}, "
            f"Price Volume Dict: {self.price_volume_dict}, "
            f"Mode: {'Long' if self.direction_int == 1 else 'Short'}")

    # def get_min_volume(self, vt_symbol):
    #     """获取最小下单数量"""
    #     symbol = vt_symbol.split(".")[0]
    #     data = settings_exchange.load_markets()
    #     symbol = symbol.replace("USDT", "/USDT")
    #     precision = data[symbol]["precision"]["amount"]
    #     return 10 ** -precision  # 最小下单数量

    def on_init(self):
        """策略初始化回调"""
        self.write_log("Init Strategy")

    def on_start(self):
        self.contract_data = self.cta_engine.main_engine.get_contract(self.vt_symbol)
        """策略启动回调"""
        self.calculate_grid_parameters()
        self.write_log(f"Calculated Parameters: Upper Price: {self.upper_price}, Bottom Price: {self.bottom_price}, "
                       f"Grid Number: {self.grid_number}, Step Price: {self.step_price}, "
                       f"Price Volume Dict: {self.price_volume_dict}")

        if not self.contract_data:
            self.write_log(f"Could Not Find The Symbol:{self.vt_symbol}, Please Connect the Api First.")
            self.inited = False
        else:
            self.inited = True

        self.pos_calculator.pos = self.pos
        self.pos_calculator.avg_price = self.avg_price

        self.cta_engine.event_engine.register(EVENT_TIMER, self.process_timer)

    def on_stop(self):
        """策略停止回调"""
        self.write_log("Stop Strategy")
        self.cta_engine.event_engine.unregister(EVENT_TIMER, self.process_timer)

    def process_timer(self, event):
        """定时器回调"""
        self.timer_count += 1
        if self.timer_count >= 10:
            self.timer_count = 0

            # 移除超出最大挂单数的订单
            if len(self.long_orders_dict.keys()) > self.max_open_orders:
                cancel_order_id = min(self.long_orders_dict.keys(), key=lambda k: self.long_orders_dict[k])
                self.cancel_order(cancel_order_id)

            if len(self.short_orders_dict.keys()) > self.max_open_orders:
                cancel_order_id = max(self.short_orders_dict.keys(), key=lambda k: self.short_orders_dict[k])
                self.cancel_order(cancel_order_id)

            self.put_event()

    def on_tick(self, tick: TickData):
        """Tick 数据回调"""
        if tick and tick.bid_price_1 > 0 and self.contract_data:
            self.tick = tick

            if self.upper_price - self.bottom_price <= 0:
                return

            # 获取当前价格
            current_price = float(self.tick.bid_price_1)

            # 检查是否达到启动价格并执行相应操作
            self.check_start_price_and_execute(current_price)

            # 如果启动价格未触发，则不执行网格交易
            if not self.start_price_triggered:
                return

            # 从 price_volume_dict 中取出当前价格上下方的网格价格
            sorted_prices = sorted(self.price_volume_dict.keys())
            lower_prices = [price for price in sorted_prices if price < current_price]
            upper_prices = [price for price in sorted_prices if price > current_price]

            # 处理多头挂单（下方网格）
            if len(self.long_orders_dict.keys()) == 0:
                for price in lower_prices[-self.max_open_orders:]:  # 取最接近当前价格的 max_open_orders 个
                    volume = self.price_volume_dict.get(price)
                    if volume is None:
                        continue  # 如果价格不在字典中，跳过

                    if price in self.fake_active_orders_price_list:
                        continue

                    # 下单
                    orders_ids = self.buy(price, volume)
                    for orderid in orders_ids:
                        self.long_orders_dict[orderid] = price

            # 处理空头挂单（上方网格）
            if len(self.short_orders_dict.keys()) == 0:
                for price in upper_prices[:self.max_open_orders]:  # 取最接近当前价格的 max_open_orders 个
                    volume = self.price_volume_dict.get(price)
                    if volume is None:
                        continue  # 如果价格不在字典中，跳过

                    if price in self.fake_active_orders_price_list:
                        continue

                    # 下单
                    orders_ids = self.short(price, volume)
                    for orderid in orders_ids:
                        self.short_orders_dict[orderid] = price
    def on_order(self, order: OrderData):
        """订单状态回调"""
        if order.vt_orderid not in (list(self.short_orders_dict.keys()) + list(self.long_orders_dict.keys())):
            return

        self.pos_calculator.update_position(order)
        self.avg_price = self.pos_calculator.avg_price
        _ContractHandler = ContractHandler(self.contract_data.price_tick)

        if order.status == Status.ALLTRADED:
            if order.vt_orderid in self.long_orders_dict.keys():
                del self.long_orders_dict[order.vt_orderid]
                self.trade_times += 1

                short_price = float(order.price) + float(self.step_price)
                if short_price <= self.upper_price:
                    short_price = _ContractHandler.process_price(short_price)
                    volume = self.price_volume_dict.get(short_price, self.order_volume)
                    orders_ids = self.short(short_price, volume, False)
                    for orderid in orders_ids:
                        self.short_orders_dict[orderid] = short_price

                if len(self.long_orders_dict.keys()) < self.max_open_orders:
                    count = len(self.long_orders_dict.keys()) + 1
                    long_price = float(order.price) - float(self.step_price) * count
                    if long_price >= self.bottom_price:
                        long_price = _ContractHandler.process_price(long_price)
                        volume = self.price_volume_dict.get(long_price, self.order_volume)
                        orders_ids = self.buy(long_price, volume, False)
                        for orderid in orders_ids:
                            self.long_orders_dict[orderid] = long_price

            if order.vt_orderid in self.short_orders_dict.keys():
                del self.short_orders_dict[order.vt_orderid]
                self.trade_times += 1

                long_price = float(order.price) - float(self.step_price)
                if long_price >= self.bottom_price:
                    long_price = _ContractHandler.process_price(long_price)
                    volume = self.price_volume_dict.get(long_price, self.order_volume)
                    orders_ids = self.buy(long_price, volume)
                    for orderid in orders_ids:
                        self.long_orders_dict[orderid] = long_price

                if len(self.short_orders_dict.keys()) < self.max_open_orders:
                    count = len(self.short_orders_dict.keys()) + 1
                    short_price = float(order.price) + float(self.step_price) * count
                    if short_price <= self.upper_price:
                        short_price = _ContractHandler.process_price(short_price)
                        volume = self.price_volume_dict.get(short_price, self.order_volume)
                        orders_ids = self.short(short_price, volume)
                        for orderid in orders_ids:
                            self.short_orders_dict[orderid] = short_price

        if not order.is_active():
            if order.vt_orderid in self.long_orders_dict.keys():
                del self.long_orders_dict[order.vt_orderid]
            elif order.vt_orderid in self.short_orders_dict.keys():
                del self.short_orders_dict[order.vt_orderid]

        self.put_event()

    def on_trade(self, trade: TradeData):
        """成交回调"""
        self.put_event()

    def check_start_price_and_execute(self, current_price: float):
        # if self.position != 0:
        #     self.start_price_triggered = True  # 存在的话，就是启动了

        """
        检查当前价格是否达到启动价格，如果是，则立即执行相应的操作。
        """
        if self.direction_int == 1:  # 多头模式
            if current_price <= self.start_price and not self.start_price_triggered:
                # 获取所有空头订单的价格和数量
                for price, volume in self.price_volume_dict.items():
                    if price > current_price:  # 空头订单的价格高于当前价格
                        # 立即买入
                        orders_ids = self.buy(price, volume)
                        for orderid in orders_ids:
                            self.long_orders_dict[orderid] = price
                self.start_price_triggered = True  # 标记启动价格已触发
        elif self.direction_int == -1:  # 空头模式
            if current_price >= self.start_price and not self.start_price_triggered:
                # 获取所有多头订单的价格和数量
                for price, volume in self.price_volume_dict.items():
                    if price < current_price:  # 多头订单的价格低于当前价格
                        # 立即卖出
                        orders_ids = self.sell(price, volume)
                        for orderid in orders_ids:
                            self.short_orders_dict[orderid] = price
                self.start_price_triggered = True  # 标记启动价格已触发