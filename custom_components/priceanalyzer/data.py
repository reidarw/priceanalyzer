import logging
import math
from datetime import datetime, timedelta
from operator import itemgetter
from statistics import mean
from datetime import date


import homeassistant.helpers.config_validation as cv
import voluptuous as vol
from homeassistant.components.sensor import PLATFORM_SCHEMA
from homeassistant.const import CONF_REGION
from homeassistant.helpers.dispatcher import async_dispatcher_connect, async_dispatcher_send
from homeassistant.helpers.entity import Entity, DeviceInfo
from homeassistant.helpers.template import Template, attach
from homeassistant.util import dt as dt_utils
from jinja2 import pass_context

from .const import DOMAIN, EVENT_NEW_DATA, _REGIONS, _PRICE_IN, EVENT_CHECKED_STUFF
from .misc import extract_attrs, has_junk, is_new, start_of

_LOGGER = logging.getLogger(__name__)

_CENT_MULTIPLIER = 100
# _PRICE_IN = {"kWh": 1000, "MWh": 0, "Wh": 1000 * 1000}
_REGIONS = {
    "DK1": ["DKK", "Denmark", 0.25],
    "DK2": ["DKK", "Denmark", 0.25],
    "FI": ["EUR", "Finland", 0.24],
    "EE": ["EUR", "Estonia", 0.20],
    "LT": ["EUR", "Lithuania", 0.21],
    "LV": ["EUR", "Latvia", 0.21],
    "Oslo": ["NOK", "Norway", 0.25],
    "Kr.sand": ["NOK", "Norway", 0.25],
    "Bergen": ["NOK", "Norway", 0.25],
    "Molde": ["NOK", "Norway", 0.25],
    "Tr.heim": ["NOK", "Norway", 0.25],
    "Tromsø": ["NOK", "Norway", 0.25],
    "SE1": ["SEK", "Sweden", 0.25],
    "SE2": ["SEK", "Sweden", 0.25],
    "SE3": ["SEK", "Sweden", 0.25],
    "SE4": ["SEK", "Sweden", 0.25],
    # What zone is this?
    "SYS": ["EUR", "System zone", 0.25],
    "FR": ["EUR", "France", 0.055],
    "NL": ["EUR", "Netherlands", 0.21],
    "BE": ["EUR", "Belgium", 0.21],
    "AT": ["EUR", "Austria", 0.20],
    # Tax is disabled for now, i need to split the areas
    # to handle the tax.
    "DE-LU": ["EUR", "Germany and Luxembourg", 0],
}

# Needed incase a user wants the prices in non local currency
_CURRENCY_TO_LOCAL = {"DKK": "Kr", "NOK": "Kr", "SEK": "Kr", "EUR": "€"}
_CURRENTY_TO_CENTS = {"DKK": "Øre", "NOK": "Øre", "SEK": "Öre", "EUR": "c"}

DEFAULT_CURRENCY = "NOK"
DEFAULT_REGION = "Kr.sand"
DEFAULT_NAME = "Elspot"


DEFAULT_TEMPLATE = "{{0.01|float}}"



class Data():
    def __init__(
        self,
        friendly_name,
        area,
        price_type,
        precision,
        low_price_cutoff,
        currency,
        vat,
        use_cents,
        api,
        ad_template,
        percent_difference,
        hass,
        config
    ) -> None:
        # friendly_name is ignored as it never worked.
        # rename the sensor in the ui if you dont like the name.
        self._attr_name = friendly_name
        self._area = area
        self._currency = currency or _REGIONS[area][0]
        self._price_type = price_type
        self._precision = precision
        self._low_price_cutoff = low_price_cutoff
        self._use_cents = use_cents
        self.api = api
        self._ad_template = ad_template
        self._hass = hass
        self.percent_difference = percent_difference or 20
        self._config = config

        if vat is True:
            self._vat = _REGIONS[area][2]
        else:
            self._vat = 0

        # Price by current hour.
        self._current_price = None

        self._current_hour = None
        self._today_calculated = None
        self._tomorrow_calculated = None


        # Holds the data for today and morrow.
        self._data_today = None
        self._data_tomorrow = None
        
        
        
        # list with sensors that utilises data from this class.
        self._sensors = []
        
        # Values for the day
        self._average = None
        self._max = None
        self._min = None
        self._off_peak_1 = None
        self._off_peak_2 = None
        self._peak = None
        self._percent_threshold = None
        self._diff = None
        self._ten_cheapest_today = None
        self._five_cheapest_today = None
        self.small_price_difference_today = None
        self.small_price_difference_tomorrow = None
        self._cheapest_hours_in_future_sorted = []

        # Values for tomorrow
        self._average_tomorrow = None
        self._max_tomorrow = None
        self._min_tomorrow = None
        self._off_peak_1_tomorrow = None
        self._off_peak_2_tomorrow = None
        self._peak_tomorrow = None
        self._percent_threshold_tomorrow = None
        self._diff_tomorrow = None
        self._ten_cheapest_tomorrow = None
        self._five_cheapest_tomorrow = None



        # Check incase the sensor was setup using config flow.
        # This blow up if the template isnt valid.
        if not isinstance(self._ad_template, Template):
            if self._ad_template in (None, ""):
                self._ad_template = DEFAULT_TEMPLATE
            self._ad_template = cv.template(self._ad_template)
        # check for yaml setup.
        else:
            if self._ad_template.template in ("", None):
                self._ad_template = cv.template(DEFAULT_TEMPLATE)

        attach(self._hass, self._ad_template)

        # To control the updates.
        self._last_tick = None
        self._cbs = []

    @property
    def device_name(self) -> str:
        return 'Priceanalyzer ' + self._area

    @property
    def device_unique_id(self):
        name = "priceanalyzer_device_%s_%s_%s_%s_%s_%s" % (
            self._price_type,
            self._area,
            self._currency,
            self._precision,
            self._low_price_cutoff,
            self._vat,
        )
        name = name.lower().replace(".", "")
        return name

    @property
    def device_info(self):
        return DeviceInfo(
            identifiers={(DOMAIN, self.device_unique_id)},
            name=self.device_name,
            manufacturer=DOMAIN,
        )

    @property
    def low_price(self) -> bool:
        """Check if the price is lower then avg depending on settings"""
        return (
            self.current_price < self._average * self._low_price_cutoff
            if self.current_price and self._average
            else None

        )

    @property
    def current_price(self) -> float:
        res = self._calc_price()
        # _LOGGER.debug("Current hours price for %s is %s", self.device_name, res)
        return res

    @property
    def today(self) -> list:
        """Get todays prices

        Returns:
            list: sorted list where today[0] is the price of hour 00.00 - 01.00
        """
        return [
            self._calc_price(i["value"], fake_dt=i["start"])
            for i in self._someday(self._data_today)
            if i
        ]

    @property
    def tomorrow(self) -> list:
        """Get tomorrows prices

        Returns:
            list: sorted where tomorrow[0] is the price of hour 00.00 - 01.00 etc.
        """
        return [
            self._calc_price(i["value"], fake_dt=i["start"])
            for i in self._someday(self._data_tomorrow)
            if i
        ]

    @property
    def current_hour(self) -> list:
        if self._today_calculated is not None:
            now = datetime.now()
            return self._today_calculated[now.hour]
        else:
            return []

    @property
    def raw_today(self):
        return self._add_raw(self._data_today)

    @property
    def raw_tomorrow(self):
        return self._add_raw(self._data_tomorrow)

    @property
    def today_calculated(self):
        if self._today_calculated != None and len(self._today_calculated):
            return self._today_calculated
        else:
            return []

    @property
    def tomorrow_calculated(self):
        if self._tomorrow_calculated != None and len(self._tomorrow_calculated):
            return self._tomorrow_calculated
        else:
            return []

    @property
    def tomorrow_valid(self):
        return len([i for i in self.tomorrow if i not in (None, float("inf"))]) >= 23


    @property
    def tomorrow_loaded(self):
        return isinstance(self._data_tomorrow, list) and len(self._data_tomorrow)

    async def _update_current_price(self) -> None:
        """ update the current price (price this hour)"""
        local_now = dt_utils.now()

        data = await self.api.today(self._area, self._currency)
        if data:
            for item in self._someday(data):
                if item["start"] == start_of(local_now, "hour"):
                    self._current_price = item["value"]
                    # _LOGGER.debug(
                    #     "Updated %s _current_price %s", self.device_name, item["value"]
                    # )


    def _someday(self, data) -> list:
        """The data is already sorted in the xml,
        but i dont trust that to continue forever. Thats why we sort it ourselfs."""
        if data is None:
            return []

        local_times = []
        for item in data.get("values", []):
            i = {
                "start": dt_utils.as_local(item["start"]),
                "end": dt_utils.as_local(item["end"]),
                "value": item["value"],
            }

            local_times.append(i)

        data["values"] = local_times

        return sorted(data.get("values", []), key=itemgetter("start"))

    def is_price_low_price(self, price) -> bool:
        """Check if the price is lower then avg depending on settings"""
        return (
            price < self._average * self._low_price_cutoff
            if price and self._average
            else None
        )

    def _is_gaining(self, hour,  now, price_now, price_next_hour, price_next_hour2, price_next_hour3, is_tomorrow) -> bool:
        threshold = self._percent_threshold_tomorrow if is_tomorrow else self._percent_threshold
        price_next_hour = max([price_next_hour,0.00001])
        price_next_hour2 = max([price_next_hour2,0.00001])
        price_next_hour3 = max([price_next_hour3,0.00001])
        percent_threshold = (1 - float(threshold))
        return ((price_now / price_next_hour) < percent_threshold) or ((price_now / price_next_hour2) < percent_threshold)# or ((price_now / price_next_hour3) < percent_threshold)

    def _is_falling(self, hour,  now, price_now, price_next_hour, price_next_hour2, price_next_hour3, is_tomorrow) -> bool:
        threshold = self._percent_threshold_tomorrow if is_tomorrow else self._percent_threshold
        percent_threshold = (1 + float(threshold))
        price_next_hour = max([price_next_hour,0.00001])
        price_next_hour2 = max([price_next_hour2,0.00001])
        price_next_hour3 = max([price_next_hour3,0.00001])
        return ((price_now / price_next_hour) > percent_threshold) or ((price_now / price_next_hour2) > percent_threshold) or ((price_now / price_next_hour3) > percent_threshold)

    def price_percent_to_average(self, item, is_tomorrow) -> float:
        """Price in percent to average price"""
        average = self._average if is_tomorrow else self._average_tomorrow
        if average is None:
            return None

        return round(item['value'] / average,3)

    def _is_falling_alot_next_hour(self, item) -> bool:
        return item['price_next_hour'] is not None and ((item['price_next_hour'] / max([item['value'],0.00001])) < 0.60)

    def _is_falling_alot_next_hours(self, item) -> bool:
        falling_alot_next_hour =  item['price_next_hour'] is not None and ((item['price_next_hour'] / max([item['value'],0.00001])) < 0.80)
        falling_alot_next_next_hour =  item['price_in_2_hours'] is not None and ((item['price_in_2_hours'] / max([item['value'],0.00001])) < 0.80)
        return falling_alot_next_hour or falling_alot_next_next_hour #todo hour after that as well.

    def _get_temperature_correction(self, item, is_tomorrow, reason = False):
        is_gaining = item['is_gaining']
        is_falling = item['is_falling']
        is_max = item['is_max']
        is_low_price = item['is_low_price']
        is_over_average = item['is_over_average']
        is_five_most_expensive = item['is_five_most_expensive']


        diff = self._diff_tomorrow if is_tomorrow else self._diff
        percent_difference = (self.percent_difference + 100) /100

        max_price = self._max_tomorrow if is_tomorrow else self._max
        threshold = self._config.get('pa_price_before_active', "") or 0
        below_threshold = float(threshold) > max_price
        if below_threshold == True:
            if reason:
                return 'Max-price below threshold'
            return 0

        #TODO this calculation is not considering additional costs.
        if(diff < percent_difference):
            if reason:
                return 'Small difference'
            return 0

        price_now = max([item["value"],0.00001])

        price_next_hour = float(item["price_next_hour"]) if item["price_next_hour"] is not None else price_now
        price_next_hour = max([price_next_hour,0.00001])

        is_over_off_peak_1 = price_now > (self._off_peak_1_tomorrow if is_tomorrow else self._off_peak_1)

        isprettycheap = price_now < 0.05        

        #TODO Check currency
        #special handling for high price at end of day:
        if not is_tomorrow and item['start'].hour == 23 and item['price_next_hour'] is not None and (price_next_hour / price_now) < 0.80 and isprettycheap == False:
            if reason:
                return 'Price dropping tomorrow'
            return -1
        if is_max:
            if reason:
                return 'Is max'
            return -1
        elif self._is_falling_alot_next_hour(item) and isprettycheap == False:
            if reason:
                return 'Is falling alot next hour'
            return -1
        elif is_gaining and (price_now < price_next_hour) and (not is_five_most_expensive):
        #TODO, When price was 128 øre at 2300, the analyzer was at +1. Why?
            
        #elif (is_over_peak == False and is_gaining == True):
        #elif (is_low_price == True and is_gaining == True):
            if reason:
                return 'Is gaining, and not in five most expensive hours'
            return 1
        elif is_falling and is_over_average == True: #TODO, check low_price_cutoff /is low price instead.
            if reason:
                return 'Is falling and is over average'
            return -1
        elif is_low_price and (not is_gaining or is_falling):
            if reason:
                return 'No need to correct.'            
            return 0
        #TODO is_over_off_peak_1 is not considering additionatla costs i think, so this is wrong.
        # Nope, the case is that it's just set hours, and not considering
        # the actual price, so useless as is. Must still get the price, and just
        # add the additional costs to get it 'more right'?
        # elif (is_over_peak and is_falling) or is_over_off_peak_1:
        #     return -1
        else:
            if reason:
                return 'No need to correct..'            
            return 0



    def get_hour(self, hour, is_tomorrow):
        if is_tomorrow == False and (hour < len(self._someday(self._data_today))):
            return self._someday(self._data_today)[hour]
        elif is_tomorrow == False and self.tomorrow_valid and (hour-24 < len(self._someday(self._data_tomorrow))):
            return self._someday(self._data_tomorrow)[hour-24]
        elif is_tomorrow and (hour < len(self._someday(self._data_tomorrow))):
            return self._someday(self._data_tomorrow)[hour]
        else:
            return None


    def _calc_price(self, value=None, fake_dt=None) -> float:
        """Calculate price based on the users settings."""
        if value is None:
            value = self._current_price

        if value is None or math.isinf(value):
            #_LOGGER.debug("api returned junk infinty %s", value)
            return None

        # Used to inject the current hour.
        # so template can be simplified using now
        if fake_dt is not None:

            def faker():
                def inner(*args, **kwargs):
                    return fake_dt

                return pass_context(inner)

            template_value = self._ad_template.async_render(now=faker())
        else:
            template_value = self._ad_template.async_render()

        # The api returns prices in MWh
        if self._price_type in ("MWh", "mWh"):
            price = template_value / 1000 + value * float(1 + self._vat)
        else:
            price = template_value + value / _PRICE_IN[self._price_type] * (
                float(1 + self._vat)
            )

        # Convert price to cents if specified by the user.
        if self._use_cents:
            price = price * _CENT_MULTIPLIER

        return round(price, self._precision)

    def _update(self, data) -> None:
        """Set attrs."""
        _LOGGER.debug("Called _update setting attrs for the day for %s", self._area)

        # if has_junk(data):
        #    # _LOGGER.debug("It was junk infinity in api response, fixed it.")
        d = extract_attrs(data.get("values"))
        data.update(d)

        if self._ad_template.template == DEFAULT_TEMPLATE:
            self._average = self._calc_price(data.get("Average"))
            self._min = self._calc_price(data.get("Min"))
            self._max = self._calc_price(data.get("Max"))
            self._off_peak_1 = self._calc_price(data.get("Off-peak 1"))
            self._off_peak_2 = self._calc_price(data.get("Off-peak 2"))
            self._peak = self._calc_price(data.get("Peak"))
            self._add_raw_calculated(False)

        else:
            data = sorted(data.get("values"), key=itemgetter("start"))
            formatted_prices = [
                self._calc_price(
                    i.get("value"), fake_dt=dt_utils.as_local(i.get("start"))
                )
                for i in data
            ]
            offpeak1 = formatted_prices[0:8]
            peak = formatted_prices[9:17]
            offpeak2 = formatted_prices[20:]

            self._peak = mean(peak)
            self._off_peak_1 = mean(offpeak1)
            self._off_peak_2 = mean(offpeak2)
            self._average = mean(formatted_prices)
            self._min = min(formatted_prices)
            self._max = max(formatted_prices)
            self._add_raw_calculated(False)



    def _update_tomorrow(self, data) -> None:

        if not self.api.tomorrow_valid():
            return
        """Set attrs."""
        _LOGGER.debug("Called _update setting attrs for the day")

        # if has_junk(data):
        #    # _LOGGER.debug("It was junk infinity in api response, fixed it.")
        d = extract_attrs(data.get("values"))
        data.update(d)

        if self._ad_template.template == DEFAULT_TEMPLATE:
            self._average_tomorrow = self._calc_price(data.get("Average"))
            self._min_tomorrow = self._calc_price(data.get("Min"))
            self._max_tomorrow = self._calc_price(data.get("Max"))
            self._off_peak_1_tomorrow = self._calc_price(data.get("Off-peak 1"))
            self._off_peak_2_tomorrow = self._calc_price(data.get("Off-peak 2"))
            self._peak_tomorrow = self._calc_price(data.get("Peak"))
            self._add_raw_calculated(True)
            # Reevaluate Today, when tomorrows prices are available
            self._add_raw_calculated(False)


        else:
            data = sorted(data.get("values"), key=itemgetter("start"))
            formatted_prices = [
                self._calc_price(
                    i.get("value"), fake_dt=dt_utils.as_local(i.get("start"))
                )
                for i in data
            ]

            if len(formatted_prices) and formatted_prices[0] is not None:
                offpeak1 = formatted_prices[0:8]
                peak = formatted_prices[9:17]
                offpeak2 = formatted_prices[20:]
                self._peak_tomorrow = mean(peak)
                self._off_peak_1_tomorrow = mean(offpeak1)
                self._off_peak_2_tomorrow = mean(offpeak2)
                self._average_tomorrow = mean(formatted_prices)
                self._min_tomorrow = min(formatted_prices)
                self._max_tomorrow = max(formatted_prices)
                self._add_raw_calculated(True)
                # Reevaluate Today, when tomorrows prices are available
                self._add_raw_calculated(False)



    def _format_time(self, data):
        local_times = []
        for item in data:
            i = {
                "start": dt_utils.as_local(item["start"]),
                "end": dt_utils.as_local(item["end"]),
                "value": item["value"],
            }

            local_times.append(i)

        return local_times


    def _add_raw(self, data):
        result = []
        for res in self._someday(data):
            item = {
                "start": res["start"],
                "end": res["end"],
                "value": self._calc_price(res["value"], fake_dt=res["start"]),
            }
            result.append(item)
        return result


    def _set_cheapest_hours_today(self):
        if self._data_today != None and len(self._data_today.get("values")):
            sorted = self.get_sorted_prices_for_day(False)
            self._ten_cheapest_today = sorted[:10] if sorted else []
            self._five_cheapest_today = sorted[:5:] if sorted else []

    def _set_cheapest_hours_tomorrow(self):
        if self._data_tomorrow != None and len(self._data_tomorrow.get("values")):

            sorted = self.get_sorted_prices_for_day(True)

            self._ten_cheapest_tomorrow = sorted[:10] if sorted else []
            self._five_cheapest_tomorrow = sorted[:5] if sorted else []
            


    def _add_raw_calculated(self, is_tomorrow):
        if is_tomorrow and self.tomorrow_valid == False:
            return []

        data = self._data_tomorrow if is_tomorrow else self._data_today
        if data is None:
            self.check_stuff()
            return
        data = sorted(data.get("values"), key=itemgetter("start"))
        _LOGGER.debug('PriceAnalyzer Adding raw calculated for %s with , %s ', self.device_name)
        result = []
        hour = 0
        percent_difference = (self.percent_difference + 100) /100
        if is_tomorrow == False:
            difference = ((self._min / self._max) - 1)
            self._percent_threshold = ((difference / 4) * -1) 
            #TODO, consider lowering thershold. for more micro-calculations.
            self._diff = self._max / max([self._min,0.00001])
            self._set_cheapest_hours_today()
            
            
            self.small_price_difference_today = (self._diff < percent_difference)


        if self.tomorrow_valid == True and is_tomorrow == True:
            max_tomorrow = self._max_tomorrow
            min_tomorrow = max([self._min_tomorrow,0.00001])
            difference = ((min_tomorrow / max_tomorrow) - 1)
            self._percent_threshold_tomorrow = ((difference / 4) * -1)
            self._diff_tomorrow = max_tomorrow / min_tomorrow
            self._set_cheapest_hours_tomorrow()
            self.small_price_difference_tomorrow = (self._diff_tomorrow < percent_difference)  

        data = self._format_time(data)
        peak = self._peak_tomorrow if is_tomorrow else self._peak
        max_price = self._max_tomorrow if is_tomorrow else self._max
        min_price = self._min_tomorrow if is_tomorrow else self._min
        average = self._average_tomorrow if is_tomorrow else self._average
        local_now = dt_utils.now()
        for res in data:

            price_now = float(self._calc_price(res["value"], fake_dt=res["start"]))
            is_max = price_now == max_price
            is_min = price_now == min_price
            is_low_price = price_now < average * self._low_price_cutoff
            is_over_peak = price_now > peak
            is_over_average = price_now > average

            next_hour = self.get_hour(hour+1, is_tomorrow)
            next_hour2 = self.get_hour(hour+2, is_tomorrow)
            next_hour3 = self.get_hour(hour+3, is_tomorrow)

            if next_hour != None and next_hour2 != None and next_hour3 != None:
                #TODO this will always be true when next day is not valid, so from 21.00 and onwards will not have price_next_hour.
                # should be better written.
                # this is fixed when next days prices are present though.
                price_next_hour3 = self._calc_price(next_hour3["value"], fake_dt=next_hour3["start"]) or price_now
                price_next_hour2 = self._calc_price(next_hour2["value"], fake_dt=next_hour2["start"]) or price_now
                price_next_hour = self._calc_price(next_hour["value"], fake_dt=next_hour["start"]) or price_now
                is_gaining = self._is_gaining(hour, res, price_now, price_next_hour, price_next_hour2, price_next_hour3, is_tomorrow)
                is_falling = self._is_falling(hour, res, price_now, price_next_hour, price_next_hour2, price_next_hour3, is_tomorrow)
            else:
                is_gaining = None
                is_falling = None
                price_next_hour = None


            item = {
                "start": res["start"],
                "end": res["end"],
                "value": price_now,
                "price_next_hour": price_next_hour,
                "price_in_2_hours": price_next_hour2,
                "is_gaining": is_gaining,
                "is_falling": is_falling,
                'is_max': is_max,
                'is_min': is_min,
                'is_low_price': is_low_price,
                'is_over_peak' : is_over_peak,
                'is_over_average': is_over_average,
            }

            is_five_most_expensive = self._is_five_most_expensive(item, is_tomorrow)
            item['price_percent_to_average'] = self.price_percent_to_average(item, is_tomorrow),
            item['is_ten_cheapest'] = self._is_ten_cheapest(item,is_tomorrow)
            item['is_five_cheapest'] = self._is_five_cheapest(item,is_tomorrow)
            item['is_five_most_expensive'] = is_five_most_expensive
            item['is_falling_a_lot_next_hour'] = self._is_falling_alot_next_hour(item)
            #Todo, add this when complete.
            #item['is_low_compared_to_tomorrow'] = self._is_low_compared_to_tomorrow(item)

            item["temperature_correction"] = self._get_temperature_correction(item, is_tomorrow)
            item["reason"] = self._get_temperature_correction(item, is_tomorrow,True)
            
            #todo is a top?
            
            hour += 1
            if item["start"] == start_of(local_now, "hour"):
                self._current_hour = item

            result.append(item)

        if is_tomorrow == False:
            self._today_calculated = result
        else:
            self._tomorrow_calculated = result


        #_LOGGER.debug('PriceAnalyzer list done rendering, %s %s', self.device_name, result)

        self.update_sensors()

        return result

    def _is_ten_cheapest(self, item, is_tomorrow):
        ten_cheapest = self._ten_cheapest_tomorrow if is_tomorrow else self._ten_cheapest_today
        if ten_cheapest == None:
            return False

        if any(obj['start'] == item['start'] for obj in ten_cheapest ):
            return True
        return False

    def _is_five_cheapest(self, item, is_tomorrow):
        five_cheapest = self._five_cheapest_tomorrow if is_tomorrow else self._five_cheapest_today
        if five_cheapest == None:
            return False

        if any(obj['start'] == item['start'] for obj in five_cheapest ):
            return True
        return False


    def _is_low_compared_to_tomorrow(self, item):
        #get the 2 cheapest hours in the future.
        future_five = self._cheapest_hours_in_future_sorted[2:]
        # if self.tomorrow_loaded:
        if any(obj['start'] == item['start'] for obj in future_five ):
            today_date = dt_utils.now().date().strftime("%d")
            item_date = dt_utils.as_local(item['start']).strftime("%d")
            _LOGGER.debug("today_date: %s, item_date: ", today_date, item_date)
            if today_date == item_date:
                return True
        return False


    def _is_five_most_expensive(self, item, is_tomorrow):
        five_most_expensive = self._get_five_most_expensive_hours(is_tomorrow)
        if any(obj['start'] == item['start'] for obj in five_most_expensive):
            return True
        return False


    def get_prices_in_future_sorted(self, reverse=True):
        return []
        #todo, fix logic here.
        

        hours_today = self._data_today
        #    hours_tomorrow = self._data_tomorrow if len(self._data_tomorrow) else []
        #TypeError: object of type 'NoneType' has no len()

        hours_tomorrow = self._data_tomorrow
        today = hours_today.get("values")
        
        
        #todo: this check does not seem to work.
        if hours_tomorrow:
            tomorrow = hours_tomorrow.get("values")
            both = tomorrow + today
        else:
            both = today


        formatted_prices = [
            {       
                    'start' : i.get('start'),
                    'end' : i.get('end'),
                    'value' : self._calc_price(
                        i.get("value"), fake_dt=dt_utils.as_local(i.get("start"))
                        )
            }
            for i in both
        ]
        
        future_prices = []
        for hour in formatted_prices:
            if dt_utils.as_local(hour.get('start')) > (dt_utils.now() - timedelta(hours=1)):
                future_prices.append(hour)
        return sorted(future_prices, key=itemgetter("value"), reverse=reverse)

    def get_sorted_prices_for_day(self, is_tomorrow, reverse=False):
        hours = self._data_tomorrow if is_tomorrow else self._data_today
        data = hours.get("values")
        if len(hours.get("values")):
            formatted_prices = [
                {
                    'start' : i.get('start'),
                    'end' : i.get('end'),
                    'value' : self._calc_price(
                        i.get("value"), fake_dt=dt_utils.as_local(i.get("start"))
                        )
                }
                for i in data
            ]
        formatted_prices = sorted(formatted_prices, key=itemgetter("value"), reverse=reverse)
        return formatted_prices
    
    def _get_five_most_expensive_hours(self, is_tomorrow):
        sorted = self.get_sorted_prices_for_day(is_tomorrow, True)
        return sorted[:5] if sorted else []



        
    def update_sensors(self):
        async_dispatcher_send(self._hass, EVENT_CHECKED_STUFF)

        

    async def check_stuff(self) -> None:
        """Cb to do some house keeping, called every hour to get the current hours price"""
        _LOGGER.debug("called check_stuff")
        if self._last_tick is None:
            self._last_tick = dt_utils.now()


        if self._data_tomorrow is None or len(self._data_tomorrow.get("values")) < 1:
            _LOGGER.debug("PriceAnalyzerSensor _data_tomorrow is none, trying to fetch it")
            tomorrow = await self.api.tomorrow(self._area, self._currency)
            if tomorrow:
                #_LOGGER.debug("PriceAnalyzerSensor FETCHED _data_tomorrow!, %s", tomorrow)
                self._data_tomorrow = tomorrow
                self._update_tomorrow(tomorrow)
            else:
                _LOGGER.debug("PriceAnalyzerSensor _data_tomorrow could not be fetched!, %s", tomorrow)
                self._data_tomorrow = None



        if self._data_today is None:
            _LOGGER.debug("PriceAnalyzerSensor _data_today is none, trying to fetch it")
            today = await self.api.today(self._area, self._currency)
            if today:
                self._data_today = today
                self._update(today)
            else:
                _LOGGER.debug("PriceAnalyzerSensor _data_today could not be fetched for %s!, %s", self._area, today)


        # We can just check if this is the first hour.
        if is_new(self._last_tick, typ="day"):
            # if now.hour == 0:
            # No need to update if we got the info we need
            if self._data_tomorrow is not None:
                self._data_today = self._data_tomorrow
                self._today_calculated = self._tomorrow_calculated
                self._average = self._average_tomorrow
                self._max = self._max_tomorrow
                self._min = self._min_tomorrow
                self._off_peak_1 = self._off_peak_1_tomorrow
                self._off_peak_2 = self._off_peak_2_tomorrow
                self._peak = self._peak_tomorrow
                self._percent_threshold = self._percent_threshold_tomorrow
                self._diff = self._diff_tomorrow
                self._ten_cheapest_today = self._ten_cheapest_tomorrow
                self._five_cheapest_today = self._five_cheapest_tomorrow

                self._update(self._data_today)
                self._data_tomorrow = None
                self._tomorrow_calculated = None
            else:
                today = await self.api.today(self._area, self._currency)
                if today:
                    self._data_today = today
                    self._update(today)

        self._cheapest_hours_in_future_sorted = self.get_prices_in_future_sorted()
        
        # Updates the current for this hour.
        await self._update_current_price()
        
        #TODO update current_hour here?


        #try to force tomorrow.
        tomorrow = await self.api.tomorrow(self._area, self._currency)
        if tomorrow:
            #often inf..
            #_LOGGER.debug("PriceAnalyzerSensor force FETCHED _data_tomorrow!, %s", tomorrow)
            self._data_tomorrow = tomorrow
            self._update_tomorrow(tomorrow)

        self._last_tick = dt_utils.now()
        self.update_sensors()
        if self.tomorrow_valid and (self._tomorrow_calculated != None and len(self._tomorrow_calculated) < 1):
            self.check_stuff()
        if self.tomorrow_valid and self._data_tomorrow == None:
            self.check_stuff()
