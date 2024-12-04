import collections
from copy import copy
import datetime
import itertools

from .utils.py3 import range, with_metaclass, iteritems

from .metabase import MetaParams
from .utils import AutoOrderedDict


# 保存订单执行相关的信息，这个信息并不能决定订单是完全或者部分执行，它仅仅保存信息
class OrderExecutionBit(object):
    '''
    Intended to hold information about order execution. A "bit" does not
    determine if the order has been fully/partially executed, it just holds
    information.

    Member Attributes:

      - dt: datetime (float) execution time
      # 执行时间 浮点数
      - size: how much was executed
      # 执行了多少
      - price: execution price
      # 执行的价格
      - closed: how much of the execution closed an existing postion
      # 现有仓位平了多少
      - opened: how much of the execution opened a new position
      # 新开仓位多少
      - openedvalue: market value of the "opened" part
      # 开仓的市值
      - closedvalue: market value of the "closed" part
      # 关闭仓位部分的市值
      - closedcomm: commission for the "closed" part
      # 关闭仓位部分的手续费
      - openedcomm: commission for the "opened" part
      # 开仓部分的手续费
      - value: market value for the entire bit size
      # 整个仓位的市值
      - comm: commission for the entire bit execution
      # 整个仓位的手续费
      - pnl: pnl generated by this bit (if something was closed)
      # 关闭部分仓位导致的盈亏
      - psize: current open position size
      # 已经开仓部分的仓位大小
      - pprice: current open position price
      # 已经开仓部分的仓位价格

    '''
    # 对订单执行信息进行初始化
    def __init__(self,
                 dt=None, size=0, price=0.0,
                 closed=0, closedvalue=0.0, closedcomm=0.0,
                 opened=0, openedvalue=0.0, openedcomm=0.0,
                 pnl=0.0,
                 psize=0, pprice=0.0):

        self.dt = dt
        self.size = size
        self.price = price

        self.closed = closed
        self.opened = opened
        self.closedvalue = closedvalue
        self.openedvalue = openedvalue
        self.closedcomm = closedcomm
        self.openedcomm = openedcomm

        self.value = closedvalue + openedvalue
        self.comm = closedcomm + openedcomm
        self.pnl = pnl

        self.psize = psize
        self.pprice = pprice

# 保存真实的订单信息以便创建和执行，创建的时候请求创建，执行的时候产出最终的结果
class OrderData(object):
    '''
    Holds actual order data for Creation and Execution.

    In the case of Creation the request made and in the case of Execution the
    actual outcome.

    Member Attributes:

      - exbits : iterable of OrderExecutionBits for this OrderData
        # 这个订单的序列化的订单执行信息
      - dt: datetime (float) creation/execution time
        # 订单创建或者执行的时间，字符串格式
      - size: requested/executed size
        # 创建或者执行的大小
      - price: execution price
        # 执行的价格，如果没有给定价格或者限价，订单创建或者当前的收盘价将会作为参考
        Note: if no price is given and no pricelimite is given, the closing
        price at the time or order creation will be used as reference
      - pricelimit: holds pricelimit for StopLimit (which has trigger first)
        # 止损限价(被先触发)的限价
      - trailamount: absolute price distance in trailing stops
        # 跟踪止损的时候的绝对价格距离
      - trailpercent: percentage price distance in trailing stops
        # 跟踪止损的时候百分比距离
      - value: market value for the entire bit size
        # 全部仓位的市值
      - comm: commission for the entire bit execution
        # 全部仓位执行的手续费
      - pnl: pnl generated by this bit (if something was closed)
        # 关闭仓位之后的盈亏
      - margin: margin incurred by the Order (if any)
        # 订单需要的保证金
      - psize: current open position size
        # 当前持仓的大小
      - pprice: current open position price
        # 当前持仓的价格

    '''
    # According to the docs, collections.deque is thread-safe with appends at
    # both ends, there will be no pop (nowhere) and therefore to know which the
    # new exbits are two indices are needed. At time of cloning (__copy__) the
    # indices can be updated to match the previous end, and the new end
    # (len(exbits)
    # Example: start 0, 0 -> islice(exbits, 0, 0) -> []
    # One added -> copy -> updated 0, 1 -> islice(exbits, 0, 1) -> [1 elem]
    # Other added -> copy -> updated 1, 2 -> islice(exbits, 1, 2) -> [1 elem]
    # "add" and "__copy__" happen always in the same thread (with all current
    # implementations) and therefore no append will happen during a copy and
    # the len of the exbits can be queried with no concerns about another
    # thread making an append and with no need for a lock

    def __init__(self, dt=None, size=0, price=0.0, pricelimit=0.0, remsize=0,
                 pclose=0.0, trailamount=0.0, trailpercent=0.0):

        self.pclose = pclose
        self.exbits = collections.deque()  # for historical purposes
        self.p1, self.p2 = 0, 0  # indices to pending notifications

        self.dt = dt
        self.size = size
        self.remsize = remsize
        self.price = price
        self.pricelimit = pricelimit
        self.trailamount = trailamount
        self.trailpercent = trailpercent
        # 如果没有限价，那么限价就使用价格
        if not pricelimit:
            # if no pricelimit is given, use the given price
            self.pricelimit = self.price
        # 如果有限价，但是没有价格，价格就等于限价
        if pricelimit and not price:
            # price must always be set if pricelimit is set ...
            self.price = pricelimit
        # 限价
        self.plimit = pricelimit

        self.value = 0.0
        self.comm = 0.0
        self.margin = None
        self.pnl = 0.0

        self.psize = 0
        self.pprice = 0

    # 设置plimit属性

    def _getplimit(self):
        return self._plimit

    def _setplimit(self, val):
        self._plimit = val

    plimit = property(_getplimit, _setplimit)

    # 返回执行信息的长度
    def __len__(self):
        return len(self.exbits)

    # 获取执行信息的某个值
    def __getitem__(self, key):
        return self.exbits[key]

    # 增加执行信息
    def add(self, dt, size, price,
            closed=0, closedvalue=0.0, closedcomm=0.0,
            opened=0, openedvalue=0.0, openedcomm=0.0,
            pnl=0.0,
            psize=0, pprice=0.0):

        self.addbit(
            OrderExecutionBit(dt, size, price,
                              closed, closedvalue, closedcomm,
                              opened, openedvalue, openedcomm, pnl,
                              psize, pprice))

    # 根据订单的执行调整当前的各个属性
    def addbit(self, exbit):
        # Stores an ExecutionBit and recalculates own values from ExBit
        self.exbits.append(exbit)

        self.remsize -= exbit.size

        self.dt = exbit.dt
        oldvalue = self.size * self.price
        newvalue = exbit.size * exbit.price
        self.size += exbit.size
        self.price = (oldvalue + newvalue) / self.size
        self.value += exbit.value
        self.comm += exbit.comm
        self.pnl += exbit.pnl
        self.psize = exbit.psize
        self.pprice = exbit.pprice

    # 获取当前等待执行信息
    def getpending(self):
        return list(self.iterpending())

    # 把订单等待执行信息切片，如果p1和p2都是等于0,似乎得到的是空的
    def iterpending(self):
        return itertools.islice(self.exbits, self.p1, self.p2)
    # 标记哪些是pending的订单执行信息
    def markpending(self):
        # rebuild the indices to mark which exbits are pending in clone
        self.p1, self.p2 = self.p2, len(self.exbits)
    # 对对象进行克隆
    def clone(self):
        self.markpending()
        obj = copy(self)
        return obj


class OrderBase(with_metaclass(MetaParams, object)):
    # 订单的基本参数
    params = (
        ('owner', None), ('data', None),
        ('size', None), ('price', None), ('pricelimit', None),
        ('exectype', None), ('valid', None), ('tradeid', 0), ('oco', None),
        ('trailamount', None), ('trailpercent', None),
        ('parent', None), ('transmit', True),
        ('simulated', False),
        # To support historical order evaluation
        ('histnotify', False),
    )
    # DAY目前代表空的时间差
    DAY = datetime.timedelta()  # constant for DAY order identification

    # Time Restrictions for orders
    # order的时间限制
    T_Close, T_Day, T_Date, T_None = range(4)

    # Volume Restrictions for orders
    # order的成交量限制
    V_None = range(1)

    # 不同的订单类型，用不同的数字表示
    (Market, Close, Limit, Stop, StopLimit, StopTrail, StopTrailLimit,
     Historical) = range(8)
    ExecTypes = ['Market', 'Close', 'Limit', 'Stop', 'StopLimit', 'StopTrail',
                 'StopTrailLimit', 'Historical']
    # 订单的方向类型
    OrdTypes = ['Buy', 'Sell']
    Buy, Sell = range(2)
    # 订单的不同状态
    Created, Submitted, Accepted, Partial, Completed, \
        Canceled, Expired, Margin, Rejected = range(9)

    Cancelled = Canceled  # alias

    Status = [
        'Created', 'Submitted', 'Accepted', 'Partial', 'Completed',
        'Canceled', 'Expired', 'Margin', 'Rejected',
    ]
    # 对每个订单增加一个数字
    refbasis = itertools.count(1)  # for a unique identifier per order

    # plimit属性的设置、获取
    def _getplimit(self):
        return self._plimit

    def _setplimit(self, val):
        self._plimit = val

    plimit = property(_getplimit, _setplimit)

    # 获取order的属性
    def __getattr__(self, name):
        # Return attr from params if not found in order
        return getattr(self.params, name)

    # 设置order的属性
    def __setattribute__(self, name, value):
        if hasattr(self.params, name):
            setattr(self.params, name, value)
        else:
            super(Order, self).__setattribute__(name, value)

    # 打印order的时候，显示出来的内容
    def __str__(self):
        tojoin = list()
        tojoin.append('Ref: {}'.format(self.ref))
        tojoin.append('OrdType: {}'.format(self.ordtype))
        tojoin.append('OrdType: {}'.format(self.ordtypename()))
        tojoin.append('Status: {}'.format(self.status))
        tojoin.append('Status: {}'.format(self.getstatusname()))
        tojoin.append('Size: {}'.format(self.size))
        tojoin.append('Price: {}'.format(self.price))
        tojoin.append('Price Limit: {}'.format(self.pricelimit))
        tojoin.append('TrailAmount: {}'.format(self.trailamount))
        tojoin.append('TrailPercent: {}'.format(self.trailpercent))
        tojoin.append('ExecType: {}'.format(self.exectype))
        tojoin.append('ExecType: {}'.format(self.getordername()))
        tojoin.append('CommInfo: {}'.format(self.comminfo))
        tojoin.append('End of Session: {}'.format(self.dteos))
        tojoin.append('Info: {}'.format(self.info))
        tojoin.append('Broker: {}'.format(self.broker))
        tojoin.append('Alive: {}'.format(self.alive()))

        return '\n'.join(tojoin)

    # 初始化类
    def __init__(self):
        # 每次创建实例的时候，都会增加一个数字
        self.ref = next(self.refbasis)
        # broker 默认是None
        self.broker = None
        # order的info信息
        self.info = AutoOrderedDict()
        # 佣金 默认是None
        self.comminfo = None
        # 触发 默认是None
        self.triggered = False
        # 如果self.parent是None的话，self._active是True,否则就是None
        self._active = self.parent is None
        # 订单状态，order初始化的时候，默认是创建
        self.status = Order.Created
        # 设置plimit属性值
        self.plimit = self.p.pricelimit  # alias via property
        # 如果订单执行类型是None的话，默认是市价单
        if self.exectype is None:
            self.exectype = Order.Market
        # 如果订单不是买单，订单的size变成负数
        if not self.isbuy():
            self.size = -self.size

        # Set a reference price if price is not set using
        # the close price
        # 如果当前不是simulated的话，pclose等于收盘价，否则等于price
        pclose = self.data.close[0] if not self.simulated else self.price
        # 如果self.price是None，并且self.pricelimit是None的话，价格等于pclose,否则价格等于self.price
        if not self.price and not self.pricelimit:
            price = pclose
        else:
            price = self.price
        # 如果不是simulated的话，订单创建时间等于当前数据的时间，否则就是0
        dcreated = self.data.datetime[0] if not self.p.simulated else 0.0
        # 订单创建
        self.created = OrderData(dt=dcreated,
                                 size=self.size,
                                 price=price,
                                 pricelimit=self.pricelimit,
                                 pclose=pclose,
                                 trailamount=self.trailamount,
                                 trailpercent=self.trailpercent)

        # Adjust price in case a trailing limit is wished
        # 如果是跟踪止损单的执行类型的话，需要对价格进行调整，限价补偿等于创建的价格减去创建的限价
        # 价格等于创建订单时候的价格，把创建订单的价格进行重新设置，如果是买单的话，设置成无限大，如果是卖单的话，设置成无线小
        # 然后调整价格；如果不是跟踪止损的类型，限价补偿是0
        if self.exectype in [Order.StopTrail, Order.StopTrailLimit]:
            self._limitoffset = self.created.price - self.created.pricelimit
            price = self.created.price
            self.created.price = float('inf' * self.isbuy() or '-inf')
            self.trailadjust(price)
        else:
            self._limitoffset = 0.0
        # 订单执行
        self.executed = OrderData(remsize=self.size)
        # 仓位设置成0
        self.position = 0
        # 接下来是对订单有效期进行判断
        # 如果有效期参数是一个时间格式的话
        if isinstance(self.valid, datetime.date):
            # comparison will later be done against the raw datetime[0] value
            # 就把时间格式转换成数字
            self.valid = self.data.date2num(self.valid)
        # 如果有效期参数是一个时间差格式，如果时间差是0的话，有效期是当天有效，否则就是当前时间加上这个时间差
        # 然后把得到的有效期转化成数字
        elif isinstance(self.valid, datetime.timedelta):
            # offset with regards to now ... get utcnow + offset
            # when reading with date2num ... it will be automatically localized
            if self.valid == self.DAY:
                valid = datetime.datetime.combine(
                    self.data.datetime.date(), datetime.time(23, 59, 59, 9999))
            else:
                valid = self.data.datetime.datetime() + self.valid

            self.valid = self.data.date2num(valid)
        # 如果有效期不是None的话，如果不是0的话，当天有效，如果是0的话，当前有效
        elif self.valid is not None:
            if not self.valid:  # avoid comparing None and 0
                valid = datetime.datetime.combine(
                    self.data.datetime.date(), datetime.time(23, 59, 59, 9999))
            else:  # assume float
                valid = self.data.datetime[0] + self.valid
        # 如果当前不是模拟的话，获取dteos，如果是模拟的话，dteos是0
        # todo 回过头好好理解下dteos具体用到了什么地方
        if not self.p.simulated:
            # provisional end-of-session
            # get next session end
            dtime = self.data.datetime.datetime(0)
            session = self.data.p.sessionend
            dteos = dtime.replace(hour=session.hour, minute=session.minute,
                                  second=session.second,
                                  microsecond=session.microsecond)

            if dteos < dtime:
                # eos before current time ... no ... must be at least next day
                dteos += datetime.timedelta(days=1)

            self.dteos = self.data.date2num(dteos)
        else:
            self.dteos = 0.0
    # 克隆order本身
    def clone(self):
        # status, triggered and executed are the only moving parts in order
        # status and triggered are covered by copy
        # executed has to be replaced with an intelligent clone of itself
        obj = copy(self)
        obj.executed = self.executed.clone()
        return obj  # status could change in next to completed
    # 获取订单状态的名称
    def getstatusname(self, status=None):
        '''Returns the name for a given status or the one of the order'''
        return self.Status[self.status if status is None else status]
    # 获取订单的名称
    def getordername(self, exectype=None):
        '''Returns the name for a given exectype or the one of the order'''
        return self.ExecTypes[self.exectype if exectype is None else exectype]

    @classmethod
    def ExecType(cls, exectype):
        return getattr(cls, exectype)
    # 获取order类型的名字
    def ordtypename(self, ordtype=None):
        '''Returns the name for a given ordtype or the one of the order'''
        return self.OrdTypes[self.ordtype if ordtype is None else ordtype]
    # 获取激活状态
    def active(self):
        return self._active
    # 激活订单
    def activate(self):
        self._active = True
    # 订单如果是创建，提交，部分成交，或者接受的状态，订单是活的
    def alive(self):
        '''Returns True if the order is in a status in which it can still be
        executed
        '''
        return self.status in [Order.Created, Order.Submitted,
                               Order.Partial, Order.Accepted]
    # 增加佣金相关的信息
    def addcomminfo(self, comminfo):
        '''Stores a CommInfo scheme associated with the asset'''
        self.comminfo = comminfo
    # 增加信息
    def addinfo(self, **kwargs):
        '''Add the keys, values of kwargs to the internal info dictionary to
        hold custom information in the order
        '''
        for key, val in iteritems(kwargs):
            self.info[key] = val
    # 判断两个订单是否相等
    def __eq__(self, other):
        return other is not None and self.ref == other.ref
    # 判断两个订单是否是不一样的
    def __ne__(self, other):
        return self.ref != other.ref
    # 判断当前是否是买订单
    def isbuy(self):
        '''Returns True if the order is a Buy order'''
        return self.ordtype == self.Buy
    # 判断当前是否是卖订单
    def issell(self):
        '''Returns True if the order is a Sell order'''
        return self.ordtype == self.Sell
    # 给订单设置具体的持仓大小
    def setposition(self, position):
        '''Receives the current position for the asset and stotres it'''
        self.position = position
    # 提交订单给broker
    def submit(self, broker=None):
        '''Marks an order as submitted and stores the broker to which it was
        submitted'''
        self.status = Order.Submitted
        self.broker = broker
        self.plen = len(self.data)
    # 接受订单
    def accept(self, broker=None):
        '''Marks an order as accepted'''
        self.status = Order.Accepted
        self.broker = broker
    # broker状态，如果broker不是None或者0之类的话，尝试从broker获取订单状态，如果broker是None，直接返回订单的彰泰
    def brokerstatus(self):
        '''Tries to retrieve the status from the broker in which the order is.

        Defaults to last known status if no broker is associated'''
        if self.broker:
            return self.broker.orderstatus(self)

        return self.status
    # 拒绝订单，如果已经拒绝了，返回False，如果不是，设置订单状态和执行拒绝的时间，broker，然后返回True
    def reject(self, broker=None):
        '''Marks an order as rejected'''
        if self.status == Order.Rejected:
            return False

        self.status = Order.Rejected
        self.executed.dt = self.data.datetime[0]
        self.broker = broker
        return True
    # 取消订单
    def cancel(self):
        '''Marks an order as cancelled'''
        self.status = Order.Canceled
        self.executed.dt = self.data.datetime[0]
    # 保证金不够，增加保证金
    def margin(self):
        '''Marks an order as having met a margin call'''
        self.status = Order.Margin
        self.executed.dt = self.data.datetime[0]
    # 完成
    def completed(self):
        '''Marks an order as completely filled'''
        self.status = self.Completed
    # 部分成交
    def partial(self):
        '''Marks an order as partially filled'''
        self.status = self.Partial
    # 执行订单
    def execute(self, dt, size, price,
                closed, closedvalue, closedcomm,
                opened, openedvalue, openedcomm,
                margin, pnl,
                psize, pprice):

        '''Receives data execution input and stores it'''
        if not size:
            return

        self.executed.add(dt, size, price,
                          closed, closedvalue, closedcomm,
                          opened, openedvalue, openedcomm,
                          pnl, psize, pprice)

        self.executed.margin = margin
    # 订单到期
    def expire(self):
        '''Marks an order as expired. Returns True if it worked'''
        self.status = self.Expired
        return True
    # 跟踪价格调整
    def trailadjust(self, price):
        pass  # generic interface

# 订单类
class Order(OrderBase):
    '''
    订单类用于保存订单创建、执行数据和订单类型
    Class which holds creation/execution data and type of oder.
    # 订单可能有下面的一些状态
    The order may have the following status:
        # 提交给broker并且等待信息
      - Submitted: sent to the broker and awaiting confirmation
        # 被broker接受
      - Accepted: accepted by the broker
        # 部分成交
      - Partial: partially executed
        # 完全成交
      - Completed: fully exexcuted
        # 取消
      - Canceled/Cancelled: canceled by the user
        # 到期
      - Expired: expired
        # 资金不足
      - Margin: not enough cash to execute the order.
        # 拒绝
      - Rejected: Rejected by the broker
        # 在订单提交的时候或者在执行之前由于现金被其他的订单使用了，可能会发生资金不足或者被拒绝的现象
        This can happen during order submission (and therefore the order will
        not reach the Accepted status) or before execution with each new bar
        price because cash has been drawn by other sources (future-like
        instruments may have reduced the cash or orders orders may have been
        executed)

    Member Attributes:
        # order的id
      - ref: unique order identifier
        # 创建的数据
      - created: OrderData holding creation data
        # 执行的数据
      - executed: OrderData holding execution data
        # 订单的信息
      - info: custom information passed over method :func:`addinfo`. It is kept
        in the form of an OrderedDict which has been subclassed, so that keys
        can also be specified using '.' notation

    User Methods:
        # 判断是否是买订单
      - isbuy(): returns bool indicating if the order buys
        # 判断是否是卖订单
      - issell(): returns bool indicating if the order sells
        # 判断订单是否是存活的，包括四种状态，创建、提交、接受、部分成交、
      - alive(): returns bool if order is in status Partial or Accepted
    '''
    # 订单的执行
    def execute(self, dt, size, price,
                closed, closedvalue, closedcomm,
                opened, openedvalue, openedcomm,
                margin, pnl,
                psize, pprice):

        super(Order, self).execute(dt, size, price,
                                   closed, closedvalue, closedcomm,
                                   opened, openedvalue, openedcomm,
                                   margin, pnl, psize, pprice)
        # 如果重新设置大小了，代表部分执行，否则代表完全成交了
        if self.executed.remsize:
            self.status = Order.Partial
        else:
            self.status = Order.Completed

        # self.comminfo = None
    # 判断订单是否到期，如果是市价单，立刻成交了，没有到期；如果是有有效期，并且当前时间大于了有效期，那么代表着订单到期了；
    def expire(self):
        if self.exectype == Order.Market:
            return False  # will be executed yes or yes

        if self.valid and self.data.datetime[0] > self.valid:
            self.status = Order.Expired
            self.executed.dt = self.data.datetime[0]
            return True

        return False
    #  移动调整价格
    def trailadjust(self, price):
        # 如果是移动数量，那么价格调整的量就是移动数量；如果是移动百分比，那么价格调整的量就是价格乘以百分比，否则价格调整的量就是0
        if self.trailamount:
            pamount = self.trailamount
        elif self.trailpercent:
            pamount = price * self.trailpercent
        else:
            pamount = 0.0

        # Stop sell is below (-), stop buy is above, move only if needed
        # 如果当前是买单的情况下，把price加上移动调整的量，如果当前的price小于创建的price,订单创建的price变成当前的价格
        # 如果执行类型是移动止损限价，那么止损限价就等于价格减去价格补偿
        if self.isbuy():
            price += pamount
            if price < self.created.price:
                self.created.price = price
                if self.exectype == Order.StopTrailLimit:
                    self.created.pricelimit = price - self._limitoffset
        # 如果是卖单, 价格减去移动调整的量，如果调整后的价格大于创建的价格，就把调整后的价格设置成订单创建时的价格
        # 如果执行类型是移动限价止损，那么止损限价就等于当前价格减去价格补偿
        else:
            price -= pamount
            if price > self.created.price:
                self.created.price = price
                if self.exectype == Order.StopTrailLimit:
                    # limitoffset is negative when pricelimit was greater
                    # the - allows increasing the price limit if stop increases
                    self.created.pricelimit = price - self._limitoffset

# 买单
class BuyOrder(Order):
    ordtype = Order.Buy

# 止损买单
class StopBuyOrder(BuyOrder):
    pass

# 创建止损限价买单
class StopLimitBuyOrder(BuyOrder):
    pass

# 创建卖单
class SellOrder(Order):
    ordtype = Order.Sell

# 创建止损埋单
class StopSellOrder(SellOrder):
    pass

# 创建止损限价卖单
class StopLimitSellOrder(SellOrder):
    pass
