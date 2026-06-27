# Sports (Formula 1) — Business Knowledge

Official LiveSQLBench knowledge base for the `sports_events_large` database:
named concepts and metric definitions used to interpret questions and write SQL.

## Race Weekend Structure
Illustrates the standard sequence of sessions that constitute a championship race weekend.

Definition: A typical race weekend consists of several sessions: up to three Free Practice sessions for teams to tune their cars, a Qualifying session to determine the starting order for the main race, and the Grand Prix Race itself. Some weekends also include a Sprint session.

## Qualifying Format Explained
Illustrates the multi-stage knockout system used in qualifying to set the race starting grid.

Definition: Qualifying is divided into three parts: Q1, Q2, and Q3. In Q1, all drivers compete, and the slowest are eliminated. The remaining drivers proceed to Q2, where more are eliminated. The final top drivers advance to Q3 to compete for Pole Position.

## Sprint Session Explained
Illustrates the concept of a Sprint session within a race weekend.

Definition: A Sprint is a shorter race held on some race weekends. It has its own abbreviated qualifying and awards fewer championship points than the main Grand Prix. Its result determines the starting grid for the main race. The inclusion of a Sprint session modifies the standard Race Weekend Structure.

## Data Unavailability for Circuit Location
Clarifies the meaning of unavailable geographical data for a circuit.

Definition: When a circuit's city, latitude, or longitude are not provided, it indicates that this information was not supplied or is unknown in the source data feed.

## Data Unavailability for Circuit Elevation
Clarifies the meaning of unavailable elevation data for a circuit.

Definition: When a circuit's elevation in meters is not provided, it signifies that this specific data point was not recorded for the circuit.

## Data Unavailability for Driver Identification
Clarifies the meaning of a missing permanent racing number or identification code for a driver.

Definition: When a driver's permanent number or three-letter identifier code is unavailable, it indicates that one has not been officially assigned or it is not present in the dataset.

## Data Unavailability for Constructor Nationality
Clarifies the meaning of unavailable nationality data for a constructor.

Definition: When a constructor's nationality is not specified, it means the team's country of origin is not recorded in the dataset.

## Indeterminate Event Timings
Clarifies the meaning of unavailable date or time information for any race weekend session.

Definition: When the date or time for any session (practice, qualifying, sprint, or race) is not provided, it signifies that the schedule for that session is To Be Determined (TBD), not applicable for the event, or not yet published.

## Championship Points System (Race)
Defines the standard points awarded for the top ten finishing positions.

Definition: Points are awarded to the top 10 finishers as follows: 1st place - 25 points, 2nd - 18, 3rd - 15, 4th - 12, 5th - 10, 6th - 8, 7th - 6, 8th - 4, 9th - 2, 10th - 1.

## Championship Points System (Sprint)
Defines the points awarded for top finishing positions in a Sprint session.

Definition: Points are awarded to the top 8 finishers in a Sprint session as follows: 1st place - 8 points, 2nd - 7, 3rd - 6, 4th - 5, 5th - 4, 6th - 3, 7th - 2, 8th - 1. This is a key feature of the Sprint Session Explained.

## Pole Position
Defines the premier starting position for a race.

Definition: A driver achieves Pole Position by setting the fastest lap time during the final stage of the Qualifying Format Explained.

## Podium Finish
Defines a top-tier race/season result for a driver.

Definition: A Podium Finish is achieved when a driver's final rank is 1st, 2nd, or 3rd.

## Points Finish
Defines a race result that earns championship points.

Definition: A Points Finish is a race classification within the top positions that are awarded points, as specified by the Championship Points System (Race).

## Fastest Lap Award
Defines the conditions for being awarded the fastest lap of a race.

Definition: The Fastest Lap Award is given to the driver who achieves the single quickest lap time during a race, under the condition that they must also secure a Points Finish.

## High-Altitude Circuit
Defines a circuit with specific environmental characteristics that impact vehicle performance.

Definition: A circuit is considered a High-Altitude Circuit if its elevation is greater than 800 meters above sea level. These circuits pose unique challenges for aerodynamics and power unit performance.

## Sprint Winner
Defines the winner of a sprint session.

Definition: A Sprint Winner is the driver who is classified in 1st place at the conclusion of a Sprint Session.

## Race Winner
Defines the winner of the main race event.

Definition: A Race Winner is the driver who is classified in 1st place at the conclusion of the main Grand Prix race.

## Efficient Pit Stop
Defines a benchmark for an exceptionally fast pit stop.

Definition: An Efficient Pit Stop is a pit stop where the total time the car is stationary is less than 2.5 seconds, indicating outstanding performance by the pit crew.

## Hat Trick
Defines a collection of three key achievements in a single race weekend.

Definition: A driver achieves a Hat Trick by securing Pole Position, being the Race Winner, and receiving the Fastest Lap Award all in the same event.

## Constructor's Double Podium
Defines a top-tier race result for a constructor (team).

Definition: A Constructor's Double Podium occurs when both drivers from the same team achieve a Podium Finish in the same race.

## Driver Age
Calculates the current age of a driver based on their birth date.

Definition: Age = \lfloor \frac{Date_{current} - Date_{birth}}{365.25} \rfloor

## Lap Time in Seconds
Converts lap time measurements from milliseconds to seconds.

Definition: T_{seconds} = \frac{T_{milliseconds}}{1000}

## Pit Stop Duration in Seconds
Converts pit stop duration from milliseconds to a more human-readable seconds format.

Definition: D_{seconds} = \frac{D_{milliseconds}}{1000}

## Driver's Average Lap Time
Calculates a driver's average lap time over the course of a race.

Definition: \bar{T}_{lap} = \frac{\sum_{i=1}^{n} T_{lap_i}}{n}, \text{where } T_{lap_i} \text{ is the Lap Time in Seconds for each lap } i \text{, and } n \text{ is the total number of laps completed by the driver.}

## Position Gain / Loss
Calculates the number of positions a driver gained or lost between the start and end of a race.

Definition: G_{pos} = P_{start} - P_{finish}, \text{where } P_{start} \text{ is the starting grid position and } P_{finish} \text{ is the final race position. A positive value indicates a gain.}

## Constructor's Total Race Points
Calculates the total points a constructor scores in a single race from both of its drivers.

Definition: P_{constructor} = \sum P_{driver}, \text{where the sum includes the points from all drivers of a constructor who had a Points Finish.}

## Driver's Points Per Race (PPR)
Calculates a driver's average points accumulation per race.

Definition: PPR = \frac{P_{total}}{R_{completed}}, \text{where } P_{total} \text{ is the driver's cumulative points and } R_{completed} \text{ is the number of races the driver has participated in.}

## Constructor Reliability Rate
Calculates the percentage of times a constructor's cars have finished the races they started.

Definition: R_{reliability} = \frac{N_{finishes}}{N_{starts}} \times 100, \text{where } N_{finishes} \text{ is the total number of times the constructor's cars were classified as finishers, and } N_{starts} \text{ is the total number of times they started a race.} A race is considered 'finished' if the status mark is not specially marked (null).

## Qualifying Time Deficit to Pole
Calculates the time difference between a driver's qualifying lap and the pole sitter's lap.

Definition: \Delta_{qualifying} = T_{driver} - T_{pole}, \text{where } T_{driver} \text{ is the driver's best Lap Time in Seconds during qualifying, and } T_{pole} \text{ is the time achieved by the driver in Pole Position.}

## Race Time Delta to Winner
Calculates the time gap between a driver's final race time and that of the winner.

Definition: \Delta_{race} = T_{driver} - T_{winner}, \text{where } T_{driver} \text{ is the driver's total race time in seconds and } T_{winner} \text{ is the total race time of the Race Winner.}

## Race Performance Index (RPI)
Calculates a driver's overall performance in a race, rewarding both a high finishing position and positions gained.

Definition: RPI = (21 - P_{finish}) + G_{pos}, \text{where } P_{finish} \text{ is the final position of the Race Winner (or other driver) and } G_{pos} \text{ is the driver's Position Gain / Loss.}

## Constructor's Performance Score (CPS)
Calculates a constructor's overall seasonal performance by weighting their points-scoring ability with their finishing reliability.

Definition: CPS = (\text{Season Total Points}) \times \frac{\text{Constructor Reliability Rate}}{100}, \text{where total points are derived from their Constructor's Total Race Points over the season.}

## Lap Time Consistency
Measures the stability of a driver's lap times during a race, calculated as the standard deviation.

Definition: LTC = \sqrt{\frac{\sum (T_{lap_i} - \bar{T}_{lap})^2}{n}}, \text{where } \bar{T}_{lap} \text{ is the driver's Driver's Average Lap Time.}

## Adjusted Race Time Delta
Calculates the race time difference to the winner after accounting for the total time the driver spent stationary during pit stops.

Definition: \Delta_{Adjusted} = \Delta_{Race} - \sum D_{seconds}, \text{where } \Delta_{Race} \text{ is the Race Time Delta to Winner and } D_{seconds} \text{ is each Pit Stop Duration in Seconds.}

## Qualifying to Race Pace Differential
Compares a driver's raw qualifying pace to their average race pace to analyze performance drop-off or improvement.

Definition: QRD = \bar{T}_{lap} - T_{qualifying}, \text{where } \bar{T}_{lap} \text{ is the Driver's Average Lap Time and } T_{qualifying} \text{ is their fastest qualifying lap from which the Qualifying Time Deficit to Pole is measured.}

## High-Altitude Performance Factor
Quantifies a driver's relative performance at circuits with unique atmospheric conditions.

Definition: HAPF = \frac{\text{Driver's Average Lap Time at High-Altitude Circuit}}{\text{Driver's Season Average Lap Time}}, \text{which evaluates performance specifically at a High-Altitude Circuit.}

## Sprint Performance Index
Calculates a driver's overall performance in a Sprint session, combining their finishing result with points scored.

Definition: SPI = (9 - P_{sprint}) + S_{pts}, \text{where } P_{sprint} \text{ is the finishing position of the Sprint Winner (or other driver) and } S_{pts} \text{ is points awarded per the Championship Points System (Sprint).}

## Driver Performance Value
Calculates a value metric for a driver by comparing their points-scoring record to their age.

Definition: DPV = \frac{\text{Driver's Points Per Race (PPR)}}{\text{Driver Age}}, \text{providing a measure of success relative to experience.}

## Team's Combined Race Result
Calculates a score for a team in a single race based on the collective finishing positions of their drivers.

Definition: TCRR = P_{finishing position of driver 1} + P_{finishing result of driver 2}. \text{A lower score is better, reaching its minimum when a team achieves a Constructor's Double Podium with a Race Winner.}

## Tyre Management Index
Estimates a driver's ability to manage tyre degradation, by comparing their lap time consistency against the number of pit stops made.

Definition: TMI = \frac{1}{\text{Lap Time Consistency} \times (1 + N_{stops})}, \text{ where a higher value is better. Uses the concept of pit stops from Efficient Pit Stop.}

## Clutch Performer
Defines a driver who excels under pressure by gaining many positions to secure a top result.

Definition: A driver is a Clutch Performer if their Position Gain / Loss is greater than 5 and they achieve a Podium Finish in the same race.

## Qualifying Specialist
Defines a driver who excels in qualifying but may not maintain the same relative pace during the race.

Definition: A driver is a Qualifying Specialist if their Qualifying Time Deficit to Pole is less than 0.2 seconds.

## Dominant Victory
Defines a particularly commanding win, characterized by a large margin over the competition.

Definition: A Dominant Victory is when a Race Winner's final Race Time Delta to Winner over the second-place driver is greater than 5 seconds.

## Strategic Masterclass
Defines a race won through superior strategy, often involving pit stops.

Definition: A Strategic Masterclass is when a Race Winner also achieves one or more Efficient Pit Stops, demonstrating that flawless strategy contributed to the victory.

## Grand Chelem
Defines the 'Grand Slam' of a race weekend, the most complete individual performance possible.

Definition: A Grand Chelem is achieved when a driver successfully completes a Hat Trick and also leads every lap of the race from start to finish.

## Reliable and Performing Constructor
Defines a team that demonstrates both exceptional reliability and strong on-track performance.

Definition: A team is a Reliable and Performing Constructor if their Constructor Reliability Rate is above 95% and their Constructor's Performance Score (CPS) is in the top three for the season.

## High-Altitude Ace
Defines a driver who shows exceptionally strong performance at high-altitude venues compared to their own baseline.

Definition: A driver is a High-Altitude Ace if their Race Performance Index (RPI) at a High-Altitude Circuit is at least 20% higher than their seasonal average RPI.

## Underdog Win
Defines a surprise victory by a driver who is not a typical front-runner based on their performance.

Definition: An Underdog Win occurs when a Race Winner has a Driver's Points Per Race (PPR) of less than 5 prior to the event.

## Flawless Team Weekend
Defines a weekend of perfect execution from both the driver and the pit crew.

Definition: A Flawless Team Weekend is when a driver secures Pole Position and is the Race Winner, and every service for their car is an Efficient Pit Stop.

## Veteran's Podium
Defines a significant achievement for an experienced, older driver.

Definition: A Veteran's Podium is when a driver with a Driver Age of 35 years or more successfully achieves a Podium Finish.

## Constructors with Significant Participation
A threshold criterion for constructors with Significant Participation in races.

Definition: Constructors are considered to have significant participation if they have started more than 10 races, ensuring statistical validity for reliability rate calculations.

## Pole-Based Race Win Probability
Defines the likelihood of a driver winning a race based on their starting position being pole.

Definition: Assume a race win probability of 35% if the driver started from Pole Position, and 5% otherwise.

## Pole-Based Fastest Lap Probability
Defines the likelihood of a driver setting the fastest lap based on their starting position being pole.

Definition: Assume a fastest lap probability of 25% if the driver started from Pole Position, and 8% otherwise.

## Qualifying Performance Cluster
Categorizes drivers based on their average qualifying deficit to pole position.

Definition: Three tiers: 'Pole Threat' (<0.15s), 'Mid Gap' (0.15s-0.4s), 'Backmarker' (≥0.4s)

## Average Stops Per Car
Calculates the average number of pit stops undertaken by each competing car in a single race.

Definition: For a given race, this is calculated as: (Total Number of Pit Stops) / (Number of Unique Cars that made a Pit Stop).

## Pit Strategy Cluster
Classifies races based on average number of pit stops per car.

Definition: Three categories: 'Single-Stop Race' (<1.5 stops), 'Standard Two-Stop' (1.5-2.5 stops), 'High-Strategy Event' (≥2.5 stops)
