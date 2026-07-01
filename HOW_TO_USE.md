**PROBE-CARD CONTACT-TEST MASK GENERATOR**



This project has two scripts that work as a pair:



&#x20; 1. io\_pair\_wiring\_parallel.py   - GENERATES the test-chip mask (GDS) from a

&#x20;                                   pinout.  

&#x20; 2. find\_missing\_probes.py       - DECODES a parallel resistance reading from a

&#x20;                                   fabricated chip to tell you which probes

&#x20;                                   on the probe card failed to make contact.



**WHAT THE TEST CHIP DOES**



Each INPUT pad on the chip is wired through a resistor of a known, unique

value to a shared central node, which is then wired to all the output pads.  

The resistor values are chosen as a binary ladder (1x, 2x, 4x, 8x, ...).  

When you land the whole probe card and measure the resistance from the input to output, you are

reading all the input resistors in parallel.  Because the values are binary,

the single parallel number tells you exactly which probes are touching and

which are open.



**ONE-TIME SETUP**



Python 3 (3.10 or newer)



One dependency, gdstk: py -m pip install gdstk



**FOLDERS**



&#x20;  inputs/          your pinout CSV goes here   (input to script #1)

&#x20;  outputs/         script #1 writes the GDS + CSV here

&#x20;  decode\_inputs/   the CSV the decoder reads   (input to script #2)

&#x20;  tests/           self-checks (optional, see bottom)





============================================================================

&#x20;**SCRIPT 1 -- GENERATE THE MASKS  (io\_pair\_wiring\_parallel.py)**

============================================================================



**STEP 1A:  Prepare the pinout CSV**



Put your probe-card pinout in the "inputs" folder



Append a column after Net Class named "I/O" (names are case-insensitive,

extra columns are ignored)                 



&#x20;  INPUTn     this pad is an input  in group n   

&#x20;  OUTPUTn    this pad is an output in group n   



Pads that share the same number n form one "group": every INPUTn is wired

through its own resistor to the OUTPUTn pads.  A group needs at least one

INPUT and one OUTPUT or it is dropped.



**STEP 1B:  Run it**



It will ask how many metal layers per chip:



&#x20;  1 = one IO group per chip   (single metal layer, no vias)

&#x20;  2 = two IO groups per chip  (second group on metal 2, via-stitched)



**STEP 1C:  What you get  (in outputs/)**



&#x20;  groups\_<n>.gds              one GDS per chip

&#x20;                              The filename lists the groups on that chip

&#x20;                              (e.g. groups\_3.0\_4.gds = group 3 part 0 + group 4).



&#x20;  pinout\_grouped\_parallel.csv every resistor's pad, signal,

&#x20;                              outputs, and its ACTUAL resistance.  The

&#x20;                              decoder needs this file (see Script 2).



&#x20;  calibration\_resistors.gds   a separate coupon with a row of big, easy-to-

&#x20;                              probe resistors at the same ladder values, used

&#x20;                              to measure the real sheet resistance.



&#x20;  calibration\_resistors.csv   the theoretical values for that coupon.



The console also prints the die size, resistor-per-edge protrusion, how many

chips were made.



============================================================================

&#x20;**SCRIPT 2 -- DECODE A MEASUREMENT  (find\_missing\_probes.py)**

============================================================================



**STEP 2A:  Give the decoder the key file**



Copy the csv file the generator (not calibration csv) into the "decode input" folder.



**STEP 2B:  Take the measurement on the bench**



Land the probe card on the chip you want to test. Measure 

the resistance from the group's INPUT to OUTPUT.  Note the

chip number, the layer number, and that resistance reading.



**STEP 2C:  Run find\_missing\_probes.py**



It will:



&#x20;  1. List the available chips.



&#x20;  2. Ask whether to apply CALIBRATION offsets (optional -- see Step 2D).

&#x20;     Answer "n" the first time if you just want a quick look.



&#x20;  3. Ask for a Chip number, then a Layer number.



&#x20;  4. Show the resistors on that coupon, then ask:

&#x20;         "Measured resistance (e.g. 470, 1.2k, OPEN):"

&#x20;     Type your reading.  Accepted formats:

&#x20;         470        470 ohms

&#x20;         1.2k       1200 ohms

&#x20;         3.3M       3.3 megaohms

&#x20;         OPEN       open circuit / over-range



&#x20;  5. Print the result:  which probes are NOT in contact, which are, and a

&#x20;     "decode margin" (how accurate your reading must be to be sure).  It

&#x20;     warns you if the answer is ambiguous or the reading looks wrong.



&#x20;**STEP 2D:  Calibration**



&#x20;  1. Probe each resistor on the calibration chip with your multimeter and

&#x20;     note the measured value of each one.

&#x20;  2. When the decoder asks "Apply calibration offsets?", answer "y".

&#x20;  3. It lists each ladder value (e.g. \~1000 ohm, \~2000 ohm, ...).  For each,

&#x20;     type the value you measured on the calibration chip.

&#x20;  4. From then on it adjusts every prediction to match your real hardware,

&#x20;     so the decode is accurate.



**TUNING**



The design knobs live at the top of io\_pair\_wiring\_parallel.py:



&#x20;  PAD\_SIZE            pad size in microns

&#x20;  WIRE\_WIDTH          resistor trace width

&#x20;  COIL\_GAP            gap from the pad to the start of its resistor

&#x20;  COIL\_BASE\_R         resistance of the smallest resistor in a group

&#x20;  MAX\_BINARY\_INPUTS   groups bigger than this are split across more chips

