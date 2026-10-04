"""Окно десктопа для перенесённых тестов — это движок веб-версии.

Тесты десктопа собирают SkitchenApp через __new__ (без __init__) и подставляют нужные
атрибуты сами, в том числе сигналы окна. В движке сигналы заменены обратными вызовами
(см. шапку core/engine.py): log_signal -> _log_callback, order_action_finished ->
on_order_action. Здесь обратный вызов связан с сигналом, который подставил тест.
"""
from engine import ProcurementEngine


def _signal_callback(callback_name, signal_name):
    def get(self):
        if callback_name in self.__dict__:
            return self.__dict__[callback_name]
        signal = self.__dict__.get(signal_name)
        return signal.emit if signal is not None else None

    def set(self, value):
        self.__dict__[callback_name] = value

    return property(get, set)


class SkitchenApp(ProcurementEngine):
    _log_callback = _signal_callback("_log_callback", "log_signal")
    on_order_action = _signal_callback("on_order_action", "order_action_finished")
