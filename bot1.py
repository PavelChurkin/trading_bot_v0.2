import requests
import pandas as pd
import numpy as np
from datetime import datetime, timedelta
import json
import time
import logging
from typing import Dict, List, Optional

# Setup logging
logging.basicConfig(level=logging.INFO, format='%(asctime)s - %(levelname)s - %(message)s')
logger = logging.getLogger(__name__)


class MoexTradingBot:
    def __init__(self):
        self.base_url = "https://iss.moex.com/iss"
        self.available_stocks = self.get_available_stocks()

    def get_available_stocks(self) -> Dict:
        """Получение списка доступных акций с MOEX"""
        logger.info("Getting available stocks from MOEX")
        url = f"{self.base_url}/engines/stock/markets/shares/boards/TQBR/securities.json"
        params = {
            'iss.only': 'securities',
            'securities.columns': 'SECID,SHORTNAME,SECNAME,PREVPRICE'  # PREVPRICE - цена предыдущего дня
        }

        try:
            response = requests.get(url, params=params)
            data = response.json()
            securities = data['securities']['data']

            stocks = {}
            for sec in securities:
                if sec[3] and sec[3] > 0:  # Фильтр по акциям с ценой
                    stocks[sec[0]] = {
                        'name': sec[1],
                        'full_name': sec[2],
                        'price': sec[3]
                    }
            logger.info(f"Loaded {len(stocks)} stocks")
            return stocks

        except Exception as e:
            logger.error(f"Error getting available stocks: {e}")
            return {}

    def search_stocks(self, query: str) -> Dict:
        """Поиск акций по названию или тикеру"""
        results = {}
        query = query.lower()

        for ticker, info in self.available_stocks.items():
            if (query in ticker.lower() or
                    query in info['name'].lower() or
                    query in info['full_name'].lower()):
                results[ticker] = info

        return results

    def get_stock_data(self, symbol: str) -> Optional[Dict]:
        """Получение текущих данных по акции"""
        logger.info(f"Getting stock data for {symbol}")
        url = f"{self.base_url}/engines/stock/markets/shares/securities/{symbol}.json"
        params = {
            'iss.only': 'marketdata',
            'marketdata.columns': 'BOARDID,LAST,OPEN,HIGH,LOW,VOLTODAY,VALTODAY,LASTTOPREVPRICE'
            # VOLTODAY - объем торгов за день, VALTODAY - стоимость торгов
        }

        try:
            response = requests.get(url, params=params, timeout=20)
            response.raise_for_status()

            if 'json' not in response.headers.get('content-type', '').lower():
                logger.error(f"Invalid response format for {symbol}")
                return None

            data = response.json()

            if 'marketdata' not in data or 'data' not in data['marketdata']:
                logger.error(f"Invalid API response structure for {symbol}")
                return None

            marketdata = data['marketdata']['data']

            # Найти данные для TQBR
            for item in marketdata:
                if len(item) < 8:
                    continue
                if item[0] == 'TQBR' and item[1] is not None and item[1] > 0:
                    logger.info(f"Got data for {symbol}: last={item[1]}")
                    return {
                        'symbol': symbol,
                        'last': item[1],
                        'open': item[2] if item[2] else item[1],
                        'high': item[3] if item[3] else item[1],
                        'low': item[4] if item[4] else item[1],
                        'volume': item[5] if item[5] else 0,
                        'value': item[6] if item[6] else 0,
                        'change': item[7] if len(item) > 7 and item[7] else 0,
                        'timestamp': datetime.now()
                    }
            logger.warning(f"No valid TQBR data found for {symbol}")
            return None

        except requests.exceptions.Timeout:
            logger.error(f"Timeout getting data for {symbol}")
            return None
        except requests.exceptions.RequestException as e:
            logger.error(f"Network error getting data for {symbol}: {e}")
            return None
        except (KeyError, IndexError, ValueError) as e:
            logger.error(f"Data parsing error for {symbol}: {e}")
            return None
        except Exception as e:
            logger.error(f"Unexpected error getting data for {symbol}: {e}")
            return None

    def get_intraday_data(self, symbol: str, interval: int = 10) -> Optional[pd.DataFrame]:
        """Получение внутридневных данных (10-минутные интервалы)"""
        logger.info(f"Getting intraday data for {symbol}")
        url = f"{self.base_url}/engines/stock/markets/shares/securities/{symbol}/candles.json"

        end_date = datetime.now()
        start_date = end_date - timedelta(hours=2)  # Данные за последние 2 часа

        params = {
            'from': start_date.strftime('%Y-%m-%d %H:%M:%S'),
            'till': end_date.strftime('%Y-%m-%d %H:%M:%S'),
            'interval': interval,  # 10-минутные интервалы
            'iss.only': 'candles',
            'candles.columns': 'open,high,low,close,volume,begin'
        }

        try:
            response = requests.get(url, params=params, timeout=10)
            if response.status_code != 200:
                return None

            data = response.json()
            if 'candles' not in data or 'data' not in data['candles']:
                return None

            candles = data['candles']['data']

            if not candles:
                return None

            df = pd.DataFrame(candles, columns=['open', 'high', 'low', 'close', 'volume', 'begin'])
            df['begin'] = pd.to_datetime(df['begin'])
            df.set_index('begin', inplace=True)

            return df

        except Exception as e:
            logger.error(f"Error getting intraday data for {symbol}: {e}")
            return None

    def get_moex_index_data(self) -> Optional[Dict]:
        """Получение данных индекса MOEX"""
        logger.info("Getting MOEX index data")
        try:
            # Получаем данные индекса MOEX
            url = f"{self.base_url}/engines/stock/markets/shares/securities/MOEX.json"
            params = {
                'iss.only': 'marketdata',
                'marketdata.columns': 'LAST,OPEN,HIGH,LOW,LASTTOPREVPRICE'
            }

            response = requests.get(url, params=params, timeout=20)
            if response.status_code != 200:
                return None

            data = response.json()
            if 'marketdata' not in data or 'data' not in data['marketdata']:
                return None

            marketdata = data['marketdata']['data']
            if not marketdata:
                return None

            item = marketdata[0]
            return {
                'symbol': 'MOEX',
                'last': item[0] if item[0] else 0,
                'open': item[1] if item[1] else 0,
                'high': item[2] if item[2] else 0,
                'low': item[3] if item[3] else 0,
                'change': item[4] if len(item) > 4 and item[4] else 0,
                'name': 'Индекс МосБиржи'
            }

        except Exception as e:
            logger.error(f"Error getting MOEX index data: {e}")
            return None

    def find_pulsating_stocks(self, min_amplitude: float = 1.0, max_amplitude: float = 5.0, limit: int = 10) -> List[
        Dict]:
        """Поиск пульсирующих акций с наибольшей амплитудой колебаний"""
        logger.info(f"Searching for pulsating stocks (amplitude: {min_amplitude}%-{max_amplitude}%)")

        pulsating_stocks = []
        checked_count = 0

        # Берем топ-100 акций по ликвидности для анализа
        top_stocks = list(self.available_stocks.keys())[:200]

        print(f"\n🔍 Анализ пульсации {len(top_stocks)} акций...")

        for symbol in top_stocks:
            try:
                # Получаем внутридневные данные
                intraday_data = self.get_intraday_data(symbol)

                if intraday_data is None or len(intraday_data) < 2:
                    continue

                # Берем последние 2 свечи
                last_candle = intraday_data.iloc[-1]
                prev_candle = intraday_data.iloc[-2]

                # Рассчитываем амплитуду для каждой свечи
                last_amplitude = ((last_candle['high'] - last_candle['low']) / last_candle['low']) * 100
                prev_amplitude = ((prev_candle['high'] - prev_candle['low']) / prev_candle['low']) * 100

                # Средняя амплитуда последних двух свечей
                avg_amplitude = (last_amplitude + prev_amplitude) / 2

                # Проверяем условие пульсации
                if min_amplitude <= avg_amplitude <= max_amplitude:
                    # Получаем текущие данные для дневного изменения
                    current_data = self.get_stock_data(symbol)
                    if current_data:
                        pulsating_stocks.append({
                            'symbol': symbol,
                            'name': self.available_stocks[symbol]['name'],
                            'amplitude': round(avg_amplitude, 2),
                            'last_amplitude': round(last_amplitude, 2),
                            'prev_amplitude': round(prev_amplitude, 2),
                            'current_price': current_data['last'],
                            'daily_change': current_data.get('change', 0),
                            'volume': current_data.get('volume', 0)
                        })

                checked_count += 1
                if checked_count % 10 == 0:
                    print(f"   Проверено {checked_count}/{len(top_stocks)} акций...")

                time.sleep(0.3)  # Пауза между запросами

            except Exception as e:
                logger.error(f"Error analyzing {symbol}: {e}")
                continue

        # Сортируем по амплитуде (по убыванию)
        pulsating_stocks.sort(key=lambda x: x['amplitude'], reverse=True)

        return pulsating_stocks[:limit]

    def get_historical_data(self, symbol: str, days: int = 30) -> Optional[pd.DataFrame]:
        """Получение исторических данных"""
        url = f"{self.base_url}/engines/stock/markets/shares/securities/{symbol}/candles.json"

        end_date = datetime.now()
        start_date = end_date - timedelta(days=days)

        params = {
            'from': start_date.strftime('%Y-%m-%d'),
            'till': end_date.strftime('%Y-%m-%d'),
            'interval': 24,
            'iss.only': 'candles',
            'candles.columns': 'open,high,low,close,volume,begin'
        }

        try:
            response = requests.get(url, params=params)
            data = response.json()
            candles = data['candles']['data']

            if not candles:
                return None

            df = pd.DataFrame(candles, columns=['open', 'high', 'low', 'close', 'volume', 'begin'])
            df['begin'] = pd.to_datetime(df['begin'])
            df.set_index('begin', inplace=True)

            return df

        except Exception as e:
            print(f"Ошибка получения исторических данных для {symbol}: {e}")
            return None

    def get_orderbook(self, symbol: str) -> Optional[Dict]:
        """Получение стакана заявок"""
        url = f"{self.base_url}/engines/stock/markets/shares/securities/{symbol}/orderbook.json"

        try:
            response = requests.get(url)
            if response.status_code != 200 or 'json' not in response.headers.get('content-type', ''):
                return None
            data = response.json()
            orderbook = data['orderbook']['data']

            bids = []
            asks = []

            for item in orderbook:
                if item[0] == 'B' and item[1] and item[2]:
                    bids.append({'price': item[1], 'quantity': item[2]})
                elif item[0] == 'S' and item[1] and item[2]:
                    asks.append({'price': item[1], 'quantity': item[2]})

            return {
                'bids': sorted(bids, key=lambda x: x['price'], reverse=True)[:10],
                'asks': sorted(asks, key=lambda x: x['price'])[:10]
            }

        except Exception as e:
            print(f"Ошибка получения стакана для {symbol}: {e}")
            return None

    def calculate_technical_levels(self, df: pd.DataFrame) -> Dict:
        """Расчет технических уровней"""
        if df.empty:
            return {}

        # Pivot Points
        high = df['high'].iloc[-1]
        low = df['low'].iloc[-1]
        close = df['close'].iloc[-1]

        pivot = (high + low + close) / 3
        r1 = 2 * pivot - low
        s1 = 2 * pivot - high
        r2 = pivot + (high - low)
        s2 = pivot - (high - low)

        # Поддержка/сопротивление по экстремумам
        window = min(10, len(df) // 3)
        supports = []
        resistances = []

        for i in range(window, len(df) - window):
            if df['low'].iloc[i] == df['low'].iloc[i - window:i + window].min():
                supports.append(df['low'].iloc[i])
            if df['high'].iloc[i] == df['high'].iloc[i - window:i + window].max():
                resistances.append(df['high'].iloc[i])

        supports = sorted(list(set([round(x, 2) for x in supports if not np.isnan(x)])))
        resistances = sorted(list(set([round(x, 2) for x in resistances if not np.isnan(x)])))

        # ATR (волатильность)
        high_low = df['high'] - df['low']
        high_close = np.abs(df['high'] - df['close'].shift())
        low_close = np.abs(df['low'] - df['close'].shift())
        true_range = np.maximum(high_low, np.maximum(high_close, low_close))
        atr = true_range.rolling(14).mean().iloc[-1] if len(true_range) > 14 else true_range.mean()

        # RSI (Relative Strength Index)
        delta = df['close'].diff()
        gain = (delta.where(delta > 0, 0)).rolling(14).mean()
        loss = (-delta.where(delta < 0, 0)).rolling(14).mean()
        rs = gain / loss
        rsi = 100 - (100 / (1 + rs))
        rsi_value = rsi.iloc[-1] if len(rsi) > 0 and not np.isnan(rsi.iloc[-1]) else 50

        # Moving Averages
        sma_20 = df['close'].rolling(20).mean().iloc[-1] if len(df) >= 20 else df['close'].mean()
        sma_50 = df['close'].rolling(50).mean().iloc[-1] if len(df) >= 50 else df['close'].mean()

        # MACD calculation
        macd_value = 0
        signal_line = 0
        macd_histogram = 0
        if len(df) >= 26:
            ema_12 = df['close'].ewm(span=12).mean()
            ema_26 = df['close'].ewm(span=26).mean()
            macd_line = ema_12 - ema_26
            signal_line = macd_line.ewm(span=9).mean().iloc[-1]
            macd_histogram = macd_line.iloc[-1] - signal_line
            macd_value = macd_line.iloc[-1]

        # Bollinger Bands
        bb_upper = close * 1.05
        bb_lower = close * 0.95
        bb_middle = close
        if len(df) >= 20:
            sma_20_bb = df['close'].rolling(20).mean()
            std_20 = df['close'].rolling(20).std()
            bb_upper = (sma_20_bb + 2 * std_20).iloc[-1]
            bb_lower = (sma_20_bb - 2 * std_20).iloc[-1]
            bb_middle = sma_20_bb.iloc[-1]

        # Stochastic Oscillator
        stoch_k = 50
        stoch_d = 50
        if len(df) >= 14:
            lowest_low = df['low'].rolling(14).min()
            highest_high = df['high'].rolling(14).max()
            if highest_high.iloc[-1] != lowest_low.iloc[-1]:
                stoch_k = 100 * ((close - lowest_low.iloc[-1]) / (highest_high.iloc[-1] - lowest_low.iloc[-1]))
                stoch_d = stoch_k  # Simplified, should be SMA of %K

        return {
            'pivot': round(pivot, 2),
            'resistance_1': round(r1, 2),
            'support_1': round(s1, 2),
            'resistance_2': round(r2, 2),
            'support_2': round(s2, 2),
            'supports': supports[-3:] if supports else [],
            'resistances': resistances[-3:] if resistances else [],
            'atr': round(atr, 2),
            'rsi': round(rsi_value, 2),
            'sma_20': round(sma_20, 2),
            'sma_50': round(sma_50, 2),
            'macd_line': round(macd_value, 4),
            'macd_signal': round(signal_line, 4),
            'macd_histogram': round(macd_histogram, 4),
            'bb_upper': round(bb_upper, 2),
            'bb_lower': round(bb_lower, 2),
            'bb_middle': round(bb_middle, 2),
            'stoch_k': round(stoch_k, 2),
            'stoch_d': round(stoch_d, 2)
        }

    def analyze_weekly_trend(self, hist_data: pd.DataFrame) -> Dict:
        """Анализ недельного тренда"""
        if hist_data.empty or len(hist_data) < 7:
            return {'trend': 'недостаточно данных', 'strength': 0}

        # Пересчет на недельные данные
        weekly = hist_data.resample('W').agg({
            'open': 'first',
            'high': 'max',
            'low': 'min',
            'close': 'last',
            'volume': 'sum'
        }).dropna()

        if len(weekly) < 2:
            return {'trend': 'недостаточно данных', 'strength': 0.0}

        # Расчет тренда по последним 4 неделям
        recent_weeks = weekly.tail(4)
        closes = recent_weeks['close']

        # Линейный тренд
        x = np.arange(len(closes))
        slope, _ = np.polyfit(x, closes, 1)

        # Определение тренда
        if slope > 0.001 * closes.iloc[0]:
            trend = 'восходящий'
            strength = min(abs(slope) / closes.iloc[0] * 100, 5)  # сила в %
        elif slope < -0.001 * closes.iloc[0]:
            trend = 'нисходящий'
            strength = min(abs(slope) / closes.iloc[0] * 100, 5)
        else:
            trend = 'боковой'
            strength = 0

        return {
            'trend': trend,
            'strength': round(strength, 2),
            'slope': round(slope, 4),
            'weeks_analyzed': len(recent_weeks)
        }

    def analyze_intentionality(self, symbol: str, current_data: Dict, hist_data: pd.DataFrame,
                               orderbook: Optional[Dict], weekly_trend: Dict, tech_levels: Dict) -> List:
        """Анализ интенциональности"""
        signals = []

        # Анализ объема
        if not hist_data.empty and current_data.get('volume'):
            volume_avg = hist_data['volume'].tail(20).mean()
            current_volume = current_data['volume']

            if current_volume > volume_avg * 2:
                signals.append("💥 СИЛЬНЫЙ ОБЪЕМ")
            elif current_volume > volume_avg * 1.5:
                signals.append("📈 ПОВЫШЕННЫЙ ОБЪЕМ")

        # Анализ стакана
        if orderbook:
            bid_volume = sum([bid['quantity'] for bid in orderbook['bids']])
            ask_volume = sum([ask['quantity'] for ask in orderbook['asks']])
            total_levels = len(orderbook['bids']) + len(orderbook['asks'])
            density = (bid_volume + ask_volume) / max(total_levels, 1) if total_levels > 0 else 0

            if bid_volume + ask_volume > 0:
                imbalance = (bid_volume - ask_volume) / (bid_volume + ask_volume)
                if imbalance > 0.3:
                    signals.append("🟢 ПРЕОБЛАДАЮТ ПОКУПКИ")
                elif imbalance < -0.3:
                    signals.append("🔴 ПРЕОБЛАДАЮТ ПРОДАЖИ")

            # Анализ плотности заявок
            if density > 10000:  # Высокая плотность
                signals.append("📊 ВЫСОКАЯ ПЛОТНОСТЬ ЗАЯВОК")
            elif density < 1000:  # Низкая плотность
                signals.append("📉 НИЗКАЯ ПЛОТНОСТЬ ЗАЯВОК")

            # Анализ спреда
            if orderbook['bids'] and orderbook['asks']:
                best_bid = max(bid['price'] for bid in orderbook['bids'])
                best_ask = min(ask['price'] for ask in orderbook['asks'])
                spread = best_ask - best_bid
                spread_pct = spread / best_bid * 100
                if spread_pct < 0.1:
                    signals.append("💰 УЗКИЙ СПРЕД (высокая ликвидность)")
                elif spread_pct > 1:
                    signals.append("📏 ШИРОКИЙ СПРЕД (низкая ликвидность)")

        # Анализ недельного тренда
        if weekly_trend['trend'] == 'восходящий':
            signals.append(f"📈 НЕДЕЛЬНЫЙ ТРЕНД ВВЕРХ (сила: {weekly_trend['strength']}%)")
        elif weekly_trend['trend'] == 'нисходящий':
            signals.append(f"📉 НЕДЕЛЬНЫЙ ТРЕНД ВНИЗ (сила: {weekly_trend['strength']}%)")

        # Анализ импульса
        if not hist_data.empty and len(hist_data) > 5:
            current_price = current_data['last']
            recent_high = hist_data['high'].tail(5).max()
            recent_low = hist_data['low'].tail(5).min()

            if current_price >= recent_high * 0.995:
                signals.append("🚀 ПРИБЛИЖЕНИЕ К МАКСИМУМАМ")
            elif current_price <= recent_low * 1.005:
                signals.append("📉 ПРИБЛИЖЕНИЕ К МИНИМУМАМ")

        # Анализ RSI
        rsi = tech_levels.get('rsi')
        if rsi:
            if rsi > 70:
                signals.append(f"⚠️ RSI ПЕРЕКУПЛЕНОСТЬ ({rsi:.1f})")
            elif rsi < 30:
                signals.append(f"⚠️ RSI ПЕРЕПРОДАННОСТЬ ({rsi:.1f})")
            elif rsi > 60:
                signals.append(f"📈 RSI ВЫШЕ 60 ({rsi:.1f})")
            elif rsi < 40:
                signals.append(f"📉 RSI НИЖЕ 40 ({rsi:.1f})")

        # Анализ MACD
        macd_line = tech_levels.get('macd_line', 0)
        macd_signal = tech_levels.get('macd_signal', 0)
        macd_histogram = tech_levels.get('macd_histogram', 0)
        if macd_line > macd_signal and macd_histogram > 0:
            signals.append("📈 MACD БЫЧИЙ СИГНАЛ")
        elif macd_line < macd_signal and macd_histogram < 0:
            signals.append("📉 MACD МЕДВЕЖИЙ СИГНАЛ")

        # Анализ Bollinger Bands
        current_price = current_data['last']
        bb_upper = tech_levels.get('bb_upper')
        bb_lower = tech_levels.get('bb_lower')
        if bb_upper and bb_lower:
            if current_price >= bb_upper:
                signals.append("⚠️ ЦЕНА У ВЕРХНЕЙ ГРАНИЦЫ BOLLINGER")
            elif current_price <= bb_lower:
                signals.append("⚠️ ЦЕНА У НИЖНЕЙ ГРАНИЦЫ BOLLINGER")

        # Анализ Stochastic
        stoch_k = tech_levels.get('stoch_k', 50)
        stoch_d = tech_levels.get('stoch_d', 50)
        if stoch_k > 80:
            signals.append(f"⚠️ STOCH ПЕРЕКУПЛЕНОСТЬ ({stoch_k:.1f})")
        elif stoch_k < 20:
            signals.append(f"⚠️ STOCH ПЕРЕПРОДАННОСТЬ ({stoch_k:.1f})")
        elif stoch_k > 70:
            signals.append(f"📈 STOCH ВЫШЕ 70 ({stoch_k:.1f})")
        elif stoch_k < 30:
            signals.append(f"📉 STOCH НИЖЕ 30 ({stoch_k:.1f})")

        # Анализ скользящих средних
        sma_20 = tech_levels.get('sma_20')
        sma_50 = tech_levels.get('sma_50')
        if sma_20 and sma_50:
            if current_price > sma_20 > sma_50:
                signals.append("📈 ЦЕНА ВЫШЕ SMA20 > SMA50")
            elif current_price > sma_20 and sma_20 < sma_50:
                signals.append("⚠️ ЦЕНА ВЫШЕ SMA20, НО SMA20 < SMA50")
            elif current_price < sma_20 < sma_50:
                signals.append("📉 ЦЕНА НИЖЕ SMA20 < SMA50")

        return signals

    def generate_trading_ranges(self, symbol: str) -> Optional[Dict]:
        """Генерация торговых диапазонов для указанной акции"""
        logger.info(f"Generating trading ranges for {symbol}")
        print(f"🔄 Анализ {symbol}...")

        # Получаем данные
        current_data = self.get_stock_data(symbol)
        if not current_data:
            logger.error(f"Failed to get current data for {symbol}")
            print(f"❌ Не удалось получить данные для {symbol}")
            return None

        hist_data = self.get_historical_data(symbol, days=30)
        if hist_data is None or hist_data.empty:
            logger.error(f"Failed to get historical data for {symbol}")
            print(f"❌ Не удалось получить исторические данные для {symbol}")
            return None

        orderbook = self.get_orderbook(symbol)
        if not orderbook:
            logger.warning(f"Orderbook not available for {symbol}")

        # Расчет уровней
        tech_levels = self.calculate_technical_levels(hist_data)
        # Анализ недельного тренда
        weekly_trend = self.analyze_weekly_trend(hist_data)
        signals = self.analyze_intentionality(symbol, current_data, hist_data, orderbook, weekly_trend, tech_levels)

        current_price = current_data['last']

        # Определяем торговые диапазоны
        buy_zone_upper = tech_levels['support_1']
        sell_zone_lower = tech_levels['resistance_1']

        # Корректируем с учетом ATR
        atr = tech_levels.get('atr', current_price * 0.02)
        buy_zone_lower = max(0, buy_zone_upper - atr * 1.5)
        sell_zone_upper = sell_zone_lower + atr * 1.5

        # Расчет комплексного скора для рекомендации
        buy_score = 0
        sell_score = 0

        # Базовый скор по позиционированию цены
        if current_price <= buy_zone_upper:
            buy_score += 30
        elif current_price >= sell_zone_lower:
            sell_score += 30

        # Анализ тренда
        if weekly_trend['trend'] == 'восходящий':
            buy_score += 20
        elif weekly_trend['trend'] == 'нисходящий':
            sell_score += 20

        # Анализ технических индикаторов
        rsi = tech_levels.get('rsi', 50)
        if rsi < 30:
            buy_score += 15  # Перепроданность
        elif rsi > 70:
            sell_score += 15  # Перекупленность

        macd_hist = tech_levels.get('macd_histogram', 0)
        if macd_hist > 0:
            buy_score += 10
        elif macd_hist < 0:
            sell_score += 10

        stoch_k = tech_levels.get('stoch_k', 50)
        if stoch_k < 20:
            buy_score += 10
        elif stoch_k > 80:
            sell_score += 10

        # Анализ скользящих средних
        sma_20 = tech_levels.get('sma_20', current_price)
        sma_50 = tech_levels.get('sma_50', current_price)
        if current_price > sma_20 > sma_50:
            buy_score += 15
        elif current_price < sma_20 < sma_50:
            sell_score += 15

        # Анализ Bollinger Bands
        bb_upper = tech_levels.get('bb_upper', current_price * 1.05)
        bb_lower = tech_levels.get('bb_lower', current_price * 0.95)
        if current_price <= bb_lower:
            buy_score += 10
        elif current_price >= bb_upper:
            sell_score += 10

        # Анализ сигналов
        bullish_signals = [s for s in signals if any(x in s for x in ['📈', '🟢', '💥', '🚀', '📊'])]
        bearish_signals = [s for s in signals if any(x in s for x in ['📉', '🔴', '⚠️'])]
        buy_score += len(bullish_signals) * 5
        sell_score += len(bearish_signals) * 5

        # Определение рекомендации
        if buy_score > sell_score + 20:
            recommendation = "🟢 ПОКУПАТЬ"
            confidence = min(0.5 + (buy_score - sell_score) / 100, 0.95)
        elif sell_score > buy_score + 20:
            recommendation = "🔴 ПРОДАВАТЬ"
            confidence = min(0.5 + (sell_score - buy_score) / 100, 0.95)
        else:
            recommendation = "🟡 ДЕРЖАТЬ"
            confidence = 0.5

        # Расчет уровней стоп-лосс и тейк-профит
        stop_loss = None
        take_profit = None
        if recommendation == "🟢 ПОКУПАТЬ":
            stop_loss = round(buy_zone_lower - atr, 2)
            take_profit = round(sell_zone_upper + atr, 2)
        elif recommendation == "🔴 ПРОДАВАТЬ":
            stop_loss = round(sell_zone_upper + atr, 2)
            take_profit = round(buy_zone_lower - atr, 2)

        return {
            'symbol': symbol,
            'name': self.available_stocks.get(symbol, {}).get('name', 'N/A'),
            'timestamp': datetime.now(),
            'current_price': current_price,
            'change': current_data.get('change', 0),
            'ranges': {
                'buy_zone': {
                    'lower': round(buy_zone_lower, 2),
                    'upper': round(buy_zone_upper, 2)
                },
                'sell_zone': {
                    'lower': round(sell_zone_lower, 2),
                    'upper': round(sell_zone_upper, 2)
                }
            },
            'risk_management': {
                'stop_loss': stop_loss,
                'take_profit': take_profit,
                'risk_reward_ratio': round(abs(take_profit - current_price) / abs(stop_loss - current_price),
                                           2) if stop_loss and take_profit else None
            },
            'technical_levels': tech_levels,
            'weekly_trend': weekly_trend,
            'signals': signals,
            'recommendation': recommendation,
            'confidence': confidence,
            'volume': current_data.get('volume', 0)
        }

    def analyze_multiple_stocks(self, symbols: List[str]) -> List[Dict]:
        """Анализ нескольких акций одновременно"""
        results = []

        for symbol in symbols:
            analysis = self.generate_trading_ranges(symbol)
            if analysis:
                results.append(analysis)
            time.sleep(0.5)  # Пауза между запросами

        # Сортируем по уверенности рекомендации
        return sorted(results, key=lambda x: x['confidence'], reverse=True)

    def print_analysis(self, analysis: Optional[Dict]):
        """Красивый вывод анализа"""
        if not analysis:
            return

        print(f"\n{'=' * 70}")
        print(f"🎯 {analysis['symbol']} - {analysis['name']}")
        print(f"{'=' * 70}")

        change_str = f"+{analysis['change']:.2f}%" if analysis['change'] > 0 else f"{analysis['change']:.2f}%"
        print(f"💰 Цена: {analysis['current_price']} RUB ({change_str})")
        print(f"📊 Решение: {analysis['recommendation']} (уверенность: {analysis['confidence']:.0%})")

        print(f"\n📈 ТОРГОВЫЕ ДИАПАЗОНЫ:")
        ranges = analysis['ranges']
        print(f"   🟢 ПОКУПКА:  {ranges['buy_zone']['lower']} - {ranges['buy_zone']['upper']} RUB")
        print(f"   🟡 НЕЙТРАЛЬНО: {ranges['buy_zone']['upper']} - {ranges['sell_zone']['lower']} RUB")
        print(f"   🔴 ПРОДАЖА: {ranges['sell_zone']['lower']} - {ranges['sell_zone']['upper']} RUB")

        print(f"\n🎯 КЛЮЧЕВЫЕ УРОВНИ:")
        levels = analysis['technical_levels']
        print(f"   Pivot: {levels['pivot']} | R1: {levels['resistance_1']} | S1: {levels['support_1']}")
        print(f"   R2: {levels.get('resistance_2', 'N/A')} | S2: {levels.get('support_2', 'N/A')}")
        if levels['supports']:
            print(f"   Поддержки: {levels['supports']}")
        if levels['resistances']:
            print(f"   Сопротивления: {levels['resistances']}")
        print(f"   ATR (волатильность): {levels['atr']}")
        if 'rsi' in levels:
            print(f"   RSI: {levels['rsi']}")
        if 'sma_20' in levels:
            print(f"   SMA20: {levels['sma_20']} | SMA50: {levels['sma_50']}")
        if 'macd_line' in levels and levels['macd_line'] != 0:
            print(
                f"   MACD: {levels['macd_line']:.4f} | Signal: {levels['macd_signal']:.4f} | Hist: {levels['macd_histogram']:.4f}")
        if 'bb_upper' in levels:
            print(f"   Bollinger: {levels['bb_lower']:.2f} - {levels['bb_middle']:.2f} - {levels['bb_upper']:.2f}")
        if 'stoch_k' in levels:
            print(f"   Stochastic: K={levels['stoch_k']:.1f} D={levels['stoch_d']:.1f}")

        trend = analysis.get('weekly_trend', {})
        if trend:
            print(f"   Недельный тренд: {trend.get('trend', 'N/A')} (сила: {trend.get('strength', 0)}%)")

        # Risk management
        risk_mgmt = analysis.get('risk_management', {})
        if risk_mgmt.get('stop_loss') and risk_mgmt.get('take_profit'):
            print(f"\n🛡️ РИСК-МЕНЕДЖМЕНТ:")
            print(f"   Stop-Loss: {risk_mgmt['stop_loss']} RUB")
            print(f"   Take-Profit: {risk_mgmt['take_profit']} RUB")
            if risk_mgmt.get('risk_reward_ratio'):
                print(f"   Risk/Reward: 1:{risk_mgmt['risk_reward_ratio']:.1f}")

        if analysis['signals']:
            print(f"\n📡 СИГНАЛЫ:")
            for signal in analysis['signals']:
                print(f"   • {signal}")

        print(f"\n📊 Объем: {analysis['volume']:,.0f}")
        print(f"🕒 Время анализа: {analysis['timestamp'].strftime('%H:%M:%S')}")
        print(f"{'=' * 70}")


# Управляющий класс для взаимодействия с пользователем
class TradingBotInterface:
    def __init__(self):
        self.bot = MoexTradingBot()
        self.favorite_stocks = []

    def show_main_menu(self):
        """Главное меню"""
        while True:
            print(f"\n{'=' * 50}")
            print("🤖 УНИВЕРСАЛЬНЫЙ ТРЕЙДИНГ БОТ MOEX")
            print(f"{'=' * 50}")
            print("1 - Анализ одной акции")
            print("2 - Анализ нескольких акций")
            print("3 - Поиск акций")
            print("4 - Мои избранные акции")
            print("5 - Топ-10 рекомендаций")
            print("6 - Поиск пульсирующих акций")
            print("7 - Выход")

            choice = input("\nВыберите действие: ").strip()

            if choice == "1":
                self.analyze_single_stock()
            elif choice == "2":
                self.analyze_multiple_stocks()
            elif choice == "3":
                self.search_stocks()
            elif choice == "4":
                self.manage_favorites()
            elif choice == "5":
                self.top_recommendations()
            elif choice == "6":
                self.find_pulsating_stocks()
            elif choice == "7":
                print("👋 До свидания!")
                break
            else:
                print("❌ Неверный выбор")

    def analyze_single_stock(self):
        """Анализ одной акции"""
        symbol = input("Введите тикер акции (например: SBER, GAZP, YNDX): ").strip().upper()

        if symbol not in self.bot.available_stocks:
            print(f"❌ Акция {symbol} не найдена или нет данных")
            return

        analysis = self.bot.generate_trading_ranges(symbol)
        self.bot.print_analysis(analysis)

        # Предлагаем добавить в избранное
        if analysis and input("Добавить в избранные? (y/n): ").lower() == 'y':
            if symbol not in self.favorite_stocks:
                self.favorite_stocks.append(symbol)
                print(f"✅ {symbol} добавлен в избранные")

    def analyze_multiple_stocks(self):
        """Анализ нескольких акций"""
        if not self.favorite_stocks:
            print("❌ Сначала добавьте акции в избранные")
            return

        print("\n📊 Анализ избранных акций:")
        for i, symbol in enumerate(self.favorite_stocks, 1):
            print(f"  {i}. {symbol} - {self.bot.available_stocks[symbol]['name']}")

        symbols_input = input("Введите номера акций через пробел (или Enter для всех): ").strip()

        if symbols_input:
            try:
                indices = [int(x) - 1 for x in symbols_input.split()]
                symbols = [self.favorite_stocks[i] for i in indices if i < len(self.favorite_stocks)]
            except:
                print("❌ Ошибка ввода")
                return
        else:
            symbols = self.favorite_stocks

        print(f"\n🔍 Анализ {len(symbols)} акций...")
        results = self.bot.analyze_multiple_stocks(symbols)

        print(f"\n📋 РЕЗУЛЬТАТЫ АНАЛИЗА:")
        for analysis in results:
            self.bot.print_analysis(analysis)

    def search_stocks(self):
        """Поиск акций"""
        query = input("Введите название или тикер для поиска: ").strip()

        if not query:
            return

        results = self.bot.search_stocks(query)

        if not results:
            print("❌ Ничего не найдено")
            return

        print(f"\n🔍 Найдено акций: {len(results)}")
        for i, (ticker, info) in enumerate(results.items(), 1):
            print(f"  {i}. {ticker} - {info['name']} - {info['price']} RUB")

        if input("\nДобавить найденные акции в избранные? (y/n): ").lower() == 'y':
            for ticker in results.keys():
                if ticker not in self.favorite_stocks:
                    self.favorite_stocks.append(ticker)
            print(f"✅ Добавлено {len(results)} акций в избранные")

    def manage_favorites(self):
        """Управление избранными акциями"""
        if not self.favorite_stocks:
            print("📝 Список избранных акций пуст")
            return

        print(f"\n⭐ МОИ ИЗБРАННЫЕ АКЦИИ ({len(self.favorite_stocks)}):")
        for i, symbol in enumerate(self.favorite_stocks, 1):
            info = self.bot.available_stocks.get(symbol, {})
            print(f"  {i}. {symbol} - {info.get('name', 'N/A')}")

        print("\n1 - Удалить акцию")
        print("2 - Очистить список")
        print("3 - Проанализировать все")

        choice = input("Выберите действие: ").strip()

        if choice == "1":
            try:
                idx = int(input("Номер акции для удаления: ")) - 1
                if 0 <= idx < len(self.favorite_stocks):
                    removed = self.favorite_stocks.pop(idx)
                    print(f"✅ {removed} удален из избранных")
            except:
                print("❌ Ошибка ввода")
        elif choice == "2":
            self.favorite_stocks.clear()
            print("✅ Список избранных очищен")
        elif choice == "3":
            self.analyze_multiple_stocks()

    def top_recommendations(self):
        """Топ-10 рекомендаций из голубых фишек"""
        blue_chips = ['SBER', 'GAZP', 'LKOH', 'ROSN', 'NVTK', 'TATN', 'GMKN', 'PLZL', 'ALRS', 'MGNT']

        print(f"\n🏆 АНАЛИЗ ГОЛУБЫХ ФИШЕК ({len(blue_chips)} акций)")
        results = self.bot.analyze_multiple_stocks(blue_chips)

        # Группируем по рекомендациям
        buy_recommendations = [r for r in results if 'ПОКУПАТЬ' in r['recommendation']]
        sell_recommendations = [r for r in results if 'ПРОДАВАТЬ' in r['recommendation']]
        hold_recommendations = [r for r in results if 'ДЕРЖАТЬ' in r['recommendation']]

        print(f"\n🟢 ПОКУПАТЬ ({len(buy_recommendations)}):")
        for analysis in sorted(buy_recommendations, key=lambda x: x['confidence'], reverse=True):
            print(f"   {analysis['symbol']} - уверенность {analysis['confidence']:.0%}")

        print(f"\n🔴 ПРОДАВАТЬ ({len(sell_recommendations)}):")
        for analysis in sorted(sell_recommendations, key=lambda x: x['confidence'], reverse=True):
            print(f"   {analysis['symbol']} - уверенность {analysis['confidence']:.0%}")

        print(f"\n🟡 ДЕРЖАТЬ ({len(hold_recommendations)}):")
        for analysis in hold_recommendations:
            print(f"   {analysis['symbol']}")

    def find_pulsating_stocks(self):
        """Поиск пульсирующих акций"""
        print(f"\n{'=' * 60}")
        print("💓 ПОИСК ПУЛЬСИРУЮЩИХ АКЦИЙ")
        print(f"{'=' * 60}")
        print("🔍 Анализ внутридневной волатильности (10-минутные интервалы)")
        print("💡 Ищем акции с амплитудой колебаний ~1.0-5%")

        # Получаем данные индекса MOEX
        moex_data = self.bot.get_moex_index_data()
        if moex_data:
            change_str = f"+{moex_data['change']:.2f}%" if moex_data['change'] > 0 else f"{moex_data['change']:.2f}%"
            print(f"\n📊 ИНДЕКС MOEX: {moex_data['last']:.2f} ({change_str})")
        else:
            print(f"\n📊 ИНДЕКС MOEX: данные недоступны")

        # Поиск пульсирующих акций
        pulsating_stocks = self.bot.find_pulsating_stocks(min_amplitude=1.0, max_amplitude=5.0, limit=20)

        if not pulsating_stocks:
            print("\n❌ Не найдено пульсирующих акций с заданными параметрами")
            return

        print(f"\n🎯 НАЙДЕНО ПУЛЬСИРУЮЩИХ АКЦИЙ: {len(pulsating_stocks)}")
        print(f"{'=' * 80}")
        print(f"{'Тикер':<10} {'Название':<25} {'Амплитуда':<12} {'Цена':<10} {'Изменение':<12} {'Объем':<12}")
        print(f"{'=' * 80}")

        for stock in pulsating_stocks:
            change_str = f"+{stock['daily_change']:.2f}%" if stock[
                                                                 'daily_change'] > 0 else f"{stock['daily_change']:.2f}%"
            volume_str = f"{stock['volume']:,.0f}" if stock['volume'] > 0 else "N/A"

            print(f"{stock['symbol']:<10} {stock['name'][:24]:<25} {stock['amplitude']:<11}% "
                  f"{stock['current_price']:<9} {change_str:<11} {volume_str:<12}")

        print(f"{'=' * 80}")

        # Детальный анализ топ-3 пульсирующих акций
        if len(pulsating_stocks) >= 3:
            print(f"\n📈 ДЕТАЛЬНЫЙ АНАЛИЗ ТОП-3 ПУЛЬСИРУЮЩИХ АКЦИЙ:")
            for i in range(min(3, len(pulsating_stocks))):
                stock = pulsating_stocks[i]
                analysis = self.bot.generate_trading_ranges(stock['symbol'])
                if analysis:
                    self.bot.print_analysis(analysis)

        # Предлагаем добавить в избранные
        if input("\nДобавить топ пульсирующие акции в избранные? (y/n): ").lower() == 'y':
            added_count = 0
            for stock in pulsating_stocks[:5]:
                if stock['symbol'] not in self.favorite_stocks:
                    self.favorite_stocks.append(stock['symbol'])
                    added_count += 1
            print(f"✅ Добавлено {added_count} акций в избранные")


# Запуск бота
if __name__ == "__main__":
    print("🚀 Загрузка универсального трейдинг бота...")

    interface = TradingBotInterface()
    interface.show_main_menu()