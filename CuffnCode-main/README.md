<p align="center">
  <img src="./images/cuffncode.png" width="200">
</p>

<h4 align="center">This project is funded by IFAC Activity Fund (July 2025 to June 2026)</h4>

__CuffnCode__ is a retrofitted blood pressure measurement system for teaching and research. In the long term, it aims to become an overinstrumented platform for developing and testing signal processing and control algorithms.

## Retrofitted pump system

<img src="./images/complete_device.png" width="600"> 

## Analog Front End Design
A reproducible, low-noise analog front end for millivolt bridge sensors (e.g., MPS20N0040D, typically used for __hobbyist__ sphygmomanometer), using AD620 instrumentation amplifier and TLC2272 level shift. This analog front end should also work for other millivolt instruments.


### TINA-TI

AC simulation with TINA-TI:

<img src="./images/AFE.png" width="600"> 

<img src="./images/tina-ac-diag.jpg" width="500"> 

Instrumentation amplifier gain:

$$ G = 1 + \frac{49.4\text{k}\Omega}{R_g} = 1 + \frac{49.4\text{k}\Omega}{470} \approx 105$$

TLC2272 offset:

$$ \frac{56 \text{k}}{47\text{k} + 56 \text{k}} \times 3.3 V \approx 1.5 V$$



### MPS20N0040D
The MPS20N0040D is a millivolt-level bridge (≈50–100 mV full-scale; 4–6 kΩ)

| <img src="./images/mps20n0040d_1.png" width="300"> | <img src="./images/mps20n0040d_2.png" width="300"> |
| ----------------------------------------- | ----------------------------------------- |

### TLC2272 (Dual, Low-Noise, Rail-To-Rail Operational Amplifier)
This will be used to offset the instrumentation amplifier, giving headroom for possible undershoot or for signal that goes both ways (positive and negative).

<img src="./images/tlc2272.png" width="300"> 

### AD620
This is the instrumentation amplifier that is relatively cheap and widely available in Indonesian market.

| <img src="./images/ad620_1.png" width="150"> | <img src="./images/ad620_2.png" width="150"> |
| ----------------------------------------- | ----------------------------------------- |

## Digital Controller
We will use STM32F411CE (the black pill) as our digital processor.

| <img src="./images/prototype1.png" width="250"> | <img src="./images/prototype2.png" width="330"> |
| ----------------------------------------- | ----------------------------------------- |

## Safety & Notes

- The MPS20N0040D is fragile—avoid over-pressure.
- If powering from USB, beware ground noise from the host PC. A ferrite on the USB cable can help.

## Next-to-Do
- 50/60 Hz notch filter (hum killer).
- PCB layouting.
- Performance evaluations.

## Credits

- Instrumentation amplifier intro: https://www.youtube.com/watch?v=O0-iczIq1aU
- INA333 review with AD620 suggestion: https://blog.robertelder.org/cjmcu-333-ina-333-instrumentation-amplifier/
- A Designer’s Guide to Instrumentation Amplifiers (3rd Edition) https://www.analog.com/media/en/training-seminars/design-handbooks/designers-guide-instrument-amps-complete.pdf

