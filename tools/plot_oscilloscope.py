import sys

from matplotlib import pyplot as plt

with open(sys.argv[1]) as f:
    data = list(map(float, f))

plt.plot(data)
plt.show()
