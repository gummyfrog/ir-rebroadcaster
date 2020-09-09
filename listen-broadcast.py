#!/usr/bin/env python

# irrp.py
# 2015-12-21
# Public Domain

"""
RECORD

--glitch     ignore edges shorter than glitch microseconds, default 100 us
--post       expect post milliseconds of silence after code, default 15 ms
--pre        expect pre milliseconds of silence before code, default 200 ms
--short      reject codes with less than short pulses, default 10
--tolerance  consider pulses the same if within tolerance percent, default 15
--no-confirm don't require a code to be repeated during record

TRANSMIT

--freq       IR carrier frequency, default 38 kHz
--gap        gap in milliseconds between transmitted codes, default 100 ms
"""

import time
import json
import os
import argparse
import schedule

import pigpio # http://abyz.co.uk/rpi/pigpio/python.html

p = argparse.ArgumentParser()

p.add_argument("-l", "--listener", help="irrp listener records", required=True)
p.add_argument("-p", "--mimic", help="irrp mimic records", required=True)
p.add_argument("-i", "--input", help="GPIO reciever", nargs="?", default=14, type=int)
p.add_argument("-o", "--output", help="GPIO led", nargs="?", default=18, type=int)

args = p.parse_args()

PRE_MS = 200
POST_MS = 15
FREQ = 38.0
GAP_MS = 100
SHORT = 10
TOLERANCE = 30
NO_CONFIRM = False
GLITCH = 100
VERBOSE = False

LISTENER   = args.listener
MIMIC      = args.mimic
INPUT      = args.input
OUTPUT     = args.output

HUES = ["red", "red1", "red2", "red3", "red4", "green1", "green2", "green3", "green4", "blue1", "blue2", "blue3", "blue4", "red1", "red"]

print("reciever on " + str(INPUT))
print("output on " + str(OUTPUT))

print("listener on " + str(LISTENER))
print("mimic on " + str(MIMIC))

POST_US    = POST_MS * 1000
PRE_US     = PRE_MS  * 1000
GAP_S      = GAP_MS  / 1000.0
CONFIRM    = not NO_CONFIRM
TOLER_MIN =  (100 - TOLERANCE) / 100.0
TOLER_MAX =  (100 + TOLERANCE) / 100.0

last_tick = 0
in_code = False
code = []
fetching_code = False


def carrier(gpio, frequency, micros):
   """
   Generate carrier square wave.
   """
   wf = []
   cycle = 1000.0 / frequency
   cycles = int(round(micros/cycle))
   on = int(round(cycle / 2.0))
   sofar = 0
   for c in range(cycles):
      target = int(round((c+1)*cycle))
      sofar += on
      off = target - sofar
      sofar += off
      wf.append(pigpio.pulse(1<<gpio, 0, on))
      wf.append(pigpio.pulse(0, 1<<gpio, off))
   return wf

def normalise(c):
   """
   Typically a code will be made up of two or three distinct
   marks (carrier) and spaces (no carrier) of different lengths.

   Because of transmission and reception errors those pulses
   which should all be x micros long will have a variance around x.

   This function identifies the distinct pulses and takes the
   average of the lengths making up each distinct pulse.  Marks
   and spaces are processed separately.

   This makes the eventual generation of waves much more efficient.

   Input

     M    S   M   S   M   S   M    S   M    S   M
   9000 4500 600 540 620 560 590 1660 620 1690 615

   Distinct marks

   9000                average 9000
   600 620 590 620 615 average  609

   Distinct spaces

   4500                average 4500
   540 560             average  550
   1660 1690           average 1675

   Output

     M    S   M   S   M   S   M    S   M    S   M
   9000 4500 609 550 609 550 609 1675 609 1675 609
   """
   if VERBOSE:
      print("before normalise", c)
   entries = len(c)
   p = [0]*entries # Set all entries not processed.
   for i in range(entries):
      if not p[i]: # Not processed?
         v = c[i]
         tot = v
         similar = 1.0

         # Find all pulses with similar lengths to the start pulse.
         for j in range(i+2, entries, 2):
            if not p[j]: # Unprocessed.
               if (c[j]*TOLER_MIN) < v < (c[j]*TOLER_MAX): # Similar.
                  tot = tot + c[j]
                  similar += 1.0

         # Calculate the average pulse length.
         newv = round(tot / similar, 2)
         c[i] = newv

         # Set all similar pulses to the average value.
         for j in range(i+2, entries, 2):
            if not p[j]: # Unprocessed.
               if (c[j]*TOLER_MIN) < v < (c[j]*TOLER_MAX): # Similar.
                  c[j] = newv
                  p[j] = 1

   if VERBOSE:
      print("after normalise", c)

def compare(p1, p2):
   if len(p1) != len(p2):
      return False

   for i in range(len(p1)):
      v = p1[i] / p2[i]
      if (v < TOLER_MIN) or (v > TOLER_MAX):
         return False

   for i in range(len(p1)):
       p1[i] = int(round((p1[i]+p2[i])/2.0))

   if VERBOSE:
      print("after compare", p1)

   return True

def end_of_code():
   global code, fetching_code
   if len(code) > SHORT:
      normalise(code)
      match = False
      for record in listener:
         if(compare(code, listener[record])):
            match = True
            print("Command: '{}'".format(record))
            lastop = record
            for i in range(1, 2) :
               play(record)

      if not match:
         print("Unknown Command!")

   code = []

def cbf(gpio, level, tick):

   global last_tick, in_code, code, fetching_code

   if level != pigpio.TIMEOUT:

      edge = pigpio.tickDiff(last_tick, tick)
      last_tick = tick

      if fetching_code:

         if (edge > PRE_US) and (not in_code): # Start of a code.
            in_code = True
            pi.set_watchdog(INPUT, POST_MS) # Start watchdog.

         elif (edge > POST_US) and in_code: # End of a code.
            in_code = False
            pi.set_watchdog(INPUT, 0) # Cancel watchdog.
            end_of_code()

         elif in_code:
            code.append(edge)

   else:
      pi.set_watchdog(INPUT, 0) # Cancel watchdog.
      if in_code:
         in_code = False
         end_of_code()


def sendWave(code, codename) :
   fetching_code = False

   pi.set_mode(OUTPUT, pigpio.OUTPUT) # IR TX connected to this GPIO.
   pi.wave_add_new()

   emit_time = time.time()

   print("P: " + codename)

   # Create wave
   marks_wid = {}
   spaces_wid = {}

   wave = [0]*len(code)

   for i in range(0, len(code)):
      ci = code[i]
      if i & 1: # Space
         if ci not in spaces_wid:
            pi.wave_add_generic([pigpio.pulse(0, 0, ci)])
            spaces_wid[ci] = pi.wave_create()
         wave[i] = spaces_wid[ci]
      else: # Mark
         if ci not in marks_wid:
            wf = carrier(OUTPUT, FREQ, ci)
            pi.wave_add_generic(wf)
            marks_wid[ci] = pi.wave_create()
         wave[i] = marks_wid[ci]

   delay = emit_time - time.time()

   if delay > 0.0:
      time.sleep(delay)

   pi.wave_chain(wave)

   if VERBOSE:
      print("key " + codename)

   while pi.wave_tx_busy():
      time.sleep(0.002)

   emit_time = time.time() + GAP_S

   for i in marks_wid:
      pi.wave_delete(marks_wid[i])

   marks_wid = {}

   for i in spaces_wid:
      pi.wave_delete(spaces_wid[i])

   spaces_wid = {}
   pi.wave_clear()
   fetching_code = True

   time.sleep(0.02)


def play(codename) :
   sendWave(mimic[codename], codename)
   sendWave(listener[codename], codename)


def flash(color):
   play(color)
   play(lastop)

def wakeup():
   print("wakeup!")
   play("on")
   play("blue")
   play("blue")

   for i in range(0, 40):
      play("dimmer")

   play("white")

   for i in range(0, 23):
      time.sleep(0.5)
      play("brighter")

def nightmode():
   play("on")
   play("blue")
   for i in range(0, 25):
      play("dimmer")

   play("white")

def off():
   play("off")
   play("off")

pi = pigpio.pi() # Connect to Pi.

if not pi.connected:
   exit(0)

try:
   l = open(LISTENER, "r")
   p = open(MIMIC, "r")
   listener = json.load(l)
   mimic = json.load(p)
   l.close()
   p.close()
except:
   listener = {}
   mimic = {}


pi.set_mode(INPUT, pigpio.INPUT) # listening on this GPIO
pi.set_glitch_filter(INPUT, GLITCH) # Ignore glitches.
cb = pi.callback(INPUT, pigpio.EITHER_EDGE, cbf)

schedule.every().day.at("06:00").do(wakeup)
schedule.every().day.at("20:00").do(nightmode)
schedule.every().day.at("00:00").do(off)

# nightmode()

print("Listening...")
fetching_code = True
code = []
lastop = "white"

while fetching_code: # listening...
   schedule.run_pending()
   time.sleep(1)

pi.stop() # Disconnect from Pi.
